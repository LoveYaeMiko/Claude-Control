"""fake_claude.py — claude CLI 的 stream-json 替身，用于端到端测试审批/多模态链路。

它模拟 `claude -p --input-format stream-json --output-format stream-json` 的行为：
从 stdin 逐行读事件，收到 user 消息后吐 `system/init`（含 session_id）与
`control_request`（subtype=can_use_tool）；收到 `control_response` 后把行为
（allow/deny）追加写进标记文件，再吐 `result` 并退出 0。

行为由环境变量驱动（继承自中继服务进程）：
- CC_FAKE_SESSION_ID   : system/init 上报的 session_id（默认随机 UUID）
- CC_FAKE_MARKER       : 追加写入行为标记的文件路径（每行 allow / deny）
- CC_FAKE_NO_PERMISSION: 置 1 则不吐 control_request（init → result 直通）
"""
# -*- coding: utf-8 -*-
import json
import os
import sys
from uuid import uuid4

try:
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
except Exception:  # pragma: no cover - 某些环境下 reconfigure 不可用
    pass


def _emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> int:
    session_id = os.environ.get("CC_FAKE_SESSION_ID", "").strip() or str(uuid4())
    marker = os.environ.get("CC_FAKE_MARKER", "").strip()
    no_perm = os.environ.get("CC_FAKE_NO_PERMISSION", "").lower() in ("1", "true", "yes")

    def mark(behavior):
        if marker:
            with open(marker, "a", encoding="utf-8") as fh:
                fh.write(behavior + "\n")

    saw_user = False
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        t = obj.get("type")
        if t == "user" and not saw_user:
            saw_user = True
            _emit({"type": "system", "subtype": "init", "session_id": session_id})
            if not no_perm:
                _emit({
                    "type": "control_request",
                    "session_id": session_id,
                    "request": {
                        "subtype": "can_use_tool",
                        "request_id": "req_001",
                        "tool_name": "Bash",
                        "display_name": "Bash",
                        "description": "运行一条 shell 命令",
                        "input": {"command": "echo hi"},
                    },
                })
        elif t == "control_response":
            # control_response.response.response.behavior（与 _control_response_line 同构）
            resp = obj.get("response") or {}
            inner = resp.get("response") or {}
            behavior = inner.get("behavior", "deny")
            mark("allow" if behavior == "allow" else "deny")
            _emit({"type": "result", "subtype": "success",
                   "result": "ok", "session_id": session_id})
            return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
