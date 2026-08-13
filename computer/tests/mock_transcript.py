"""生成模拟的 Claude Code transcript JSONL，用于端到端测试。"""
# -*- coding: utf-8 -*-
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

VERSION = "2.1.224"
CWD = r"c:\Users\wyxwi\Desktop\Claude-Control"
MODEL = "deepseek-v4-pro"


def _ts(dt=None):
    dt = dt or datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + ".000Z"


def _base(rtype, session_id, **extra):
    o = {"type": rtype, "cwd": CWD, "entrypoint": "claude", "gitBranch": "main",
         "isSidechain": False, "sessionId": session_id, "uuid": str(uuid4()),
         "timestamp": _ts(), "version": VERSION}
    o.update(extra)
    return o


def user_prompt(session_id, text):
    o = _base("user", session_id)
    o["message"] = {"role": "user", "content": [{"type": "text", "text": text}]}
    return o


def assistant_text(session_id, text, model=MODEL, thinking=None):
    content = []
    if thinking:
        content.append({"type": "thinking", "thinking": thinking, "signature": str(uuid4())})
    content.append({"type": "text", "text": text})
    o = _base("assistant", session_id)
    o["message"] = {"role": "assistant", "id": str(uuid4()),
                    "content": content, "model": model,
                    "stop_reason": "end_turn", "stop_sequence": None,
                    "usage": {"input_tokens": 100, "output_tokens": 50}}
    return o


def assistant_tool_use(session_id, tool_name, tool_input, tool_id="call_01"):
    o = _base("assistant", session_id)
    o["message"] = {"role": "assistant", "id": str(uuid4()),
                    "content": [{"type": "tool_use", "id": tool_id,
                                 "name": tool_name, "input": tool_input}],
                    "model": MODEL, "stop_reason": "tool_use",
                    "usage": {"input_tokens": 200, "output_tokens": 30}}
    return o


def user_tool_result(session_id, tool_use_id, output, is_error=False):
    o = _base("user", session_id)
    o["message"] = {"role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": tool_use_id,
                                 "content": output, "is_error": is_error}]}
    return o


def ai_title(session_id, title):
    o = _base("ai-title", session_id)
    o["aiTitle"] = title
    return o


def last_prompt(session_id, prompt):
    o = _base("last-prompt", session_id)
    o["lastPrompt"] = prompt
    return o


def system_compact(session_id):
    o = _base("system", session_id)
    o["subtype"] = "compact_boundary"
    o["content"] = "context compaction"
    return o


def write_transcript(path: Path, session_id: str, lines=None):
    """写入 transcript（追加模式）。lines 为空则写一段典型会话。"""
    if lines is None:
        lines = [
            user_prompt(session_id, "帮我初始化项目配置"),
            assistant_text(session_id, "好的，我先分析现有代码结构。",
                           thinking="用户希望初始化项目，先从探索代码开始。"),
            assistant_tool_use(session_id, "Bash", {"command": "ls -la"}, tool_id="call_01"),
            user_tool_result(session_id, "call_01", "blueprint.md\ncomputer/relay_server.py", is_error=False),
            assistant_text(session_id, "分析完成，开始配置……"),
            ai_title(session_id, "初始化项目配置"),
            last_prompt(session_id, "帮我初始化项目配置"),
        ]
    with open(path, "a", encoding="utf-8") as fh:
        for ln in lines:
            fh.write(json.dumps(ln, ensure_ascii=False) + "\n")
        fh.flush()
        fh.close()


def make_mock_project(root: Path) -> Path:
    """在 root 下生成 <slug>/<session>.jsonl，返回 session_id。"""
    slug = "c--Users-wyxwi-Desktop-Claude-Control"
    session_id = str(uuid4())
    proj_dir = root / slug
    proj_dir.mkdir(parents=True, exist_ok=True)
    write_transcript(proj_dir / f"{session_id}.jsonl", session_id)
    return session_id
