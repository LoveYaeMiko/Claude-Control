import atexit
import base64
import datetime
import json
import os
import signal
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time


def clean(value, limit=500):
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    if not value:
        return None
    return value[:limit] + ("…" if len(value) > limit else "")


def parse_time(value):
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def json_line(raw):
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None


def prompt_from_record(record):
    if not isinstance(record, dict):
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    if record.get("type") == "event_msg" and payload.get("type") == "user_message":
        return clean(payload.get("message"))
    if (record.get("type") != "response_item"
            or payload.get("type") != "message"
            or payload.get("role") != "user"):
        return None
    content = payload.get("content")
    if isinstance(content, str):
        return clean(content)
    if not isinstance(content, list):
        return None
    text = " ".join(
        item.get("text", "")
        for item in content
        if isinstance(item, dict)
        and item.get("type") in ("input_text", "text")
        and isinstance(item.get("text"), str)
    )
    return clean(text)


def decode_argument(value):
    if not isinstance(value, str):
        return ""
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode((value + padding).encode("ascii")).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ""


def latest_prompt(path):
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            position = handle.tell()
            carry = b""
            while position > 0:
                start = max(0, position - 262144)
                handle.seek(start)
                block = handle.read(position - start) + carry
                parts = block.split(b"\n")
                carry = parts[0]
                for raw in reversed(parts[1:]):
                    prompt = prompt_from_record(json_line(raw))
                    if prompt:
                        return prompt
                position = start
            return prompt_from_record(json_line(carry)) if carry else None
    except OSError:
        return None


def process_snapshot(sessions_root):
    active_paths = {}
    children = {}
    commands = {}
    parents = {}
    ttys = {}
    proc_root = "/proc"
    if not os.path.isdir(proc_root):
        try:
            output = subprocess.run(
                ["lsof", "-F", "pn", "+D", sessions_root],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=5
            ).stdout
            pid = None
            for line in output.splitlines():
                if line.startswith("p"):
                    pid = int(line[1:])
                elif line.startswith("n") and line.endswith(".jsonl") and pid:
                    active_paths[os.path.realpath(line[1:])] = pid
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        try:
            output = subprocess.run(
                ["ps", "-axo", "pid=,ppid=,tty=,command="],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=5
            ).stdout
            for line in output.splitlines():
                fields = line.strip().split(None, 3)
                if len(fields) < 4:
                    continue
                pid = int(fields[0])
                parent = int(fields[1])
                children.setdefault(parent, []).append(pid)
                parents[pid] = parent
                ttys[pid] = fields[2]
                commands[pid] = fields[3]
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        return active_paths, children, commands, parents, ttys
    try:
        process_ids = [name for name in os.listdir(proc_root) if name.isdigit()]
    except OSError:
        return active_paths, children, commands, parents, ttys
    for name in process_ids:
        pid = int(name)
        try:
            with open(os.path.join(proc_root, name, "stat"), "r", encoding="utf-8") as handle:
                stat = handle.read()
            close = stat.rfind(")")
            fields = stat[close + 2:].split()
            parent = int(fields[1])
            command = stat[stat.find("(") + 1:close]
            children.setdefault(parent, []).append(pid)
            parents[pid] = parent
            try:
                with open(os.path.join(proc_root, name, "cmdline"), "rb") as handle:
                    command_line = handle.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
                commands[pid] = command_line or command
            except OSError:
                commands[pid] = command
        except (OSError, ValueError, IndexError):
            pass
        fd_root = os.path.join(proc_root, name, "fd")
        try:
            tty = os.path.realpath(os.path.join(fd_root, "0"))
            if tty.startswith("/dev/"):
                ttys[pid] = tty[5:]
        except OSError:
            pass
        try:
            for fd in os.listdir(fd_root):
                try:
                    target = os.path.realpath(os.path.join(fd_root, fd))
                    if target.startswith(sessions_root + os.sep) and target.endswith(".jsonl"):
                        active_paths[target] = pid
                except OSError:
                    pass
        except OSError:
            pass
    return active_paths, children, commands, parents, ttys


def writer_context(pid, parents, commands, ttys):
    current = pid
    seen = set()
    tty = None
    owner = None
    while current and current not in seen and len(seen) < 16:
        seen.add(current)
        command = commands.get(current, "")
        lowered = command.lower()
        candidate_tty = ttys.get(current)
        if not tty and candidate_tty not in (None, "?", "??", "-"):
            tty = candidate_tty
        if ".vscode-server" in lowered or "ptyhost" in lowered:
            owner = "VS Code 远程终端"
            break
        if ".cursor-server" in lowered:
            owner = "Cursor 远程终端"
            break
        if "tmux" in lowered:
            owner = "tmux"
            break
        if "screen" in lowered:
            owner = "screen"
            break
        if "sshd:" in lowered:
            owner = "SSH 终端"
        current = parents.get(current)
    if owner is None:
        owner = "远程终端"
    return owner, tty


def has_working_child(pid, children, commands):
    queue = list(children.get(pid, []))
    visited = set()
    helpers = {"codex", "node", "codex-code-mode-host"}
    while queue:
        child = queue.pop()
        if child in visited:
            continue
        visited.add(child)
        if commands.get(child, "").lower() not in helpers:
            return True
        queue.extend(children.get(child, []))
    return False


def attention_detail(tool_name):
    name = tool_name.lower()
    if "request_user_input" in name:
        return "Codex 正在等待你的选择"
    if "permission" in name:
        return "Codex 正在请求权限"
    if "apply_patch" in name or "write" in name:
        return "文件修改等待确认"
    if "mcp" in name:
        return "外部工具调用等待确认"
    return "命令执行等待确认"


def needs_attention(call, approval_mode, working_child, now):
    name = call.get("name", "").lower()
    started_at = call.get("started_at")
    if started_at and now - started_at < 1.2:
        return False
    if "request_user_input" in name or "requestpermission" in name:
        return True
    if str(approval_mode).lower() == "never" or working_child:
        return False
    return any(part in name for part in ("exec", "shell", "apply_patch", "write", "permission", "mcp"))


def detect_state(path, approval_mode, active_pid, working_child, modified_at, now):
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 524288))
            data = handle.read()
        if size > len(data) and b"\n" in data:
            data = data.split(b"\n", 1)[1]
    except OSError:
        state = "active" if active_pid else "failed"
        return state, "Codex 正在运行" if active_pid else "会话记录不可读", modified_at, None, None

    lines = data.splitlines()
    unfinished = False
    found_boundary = False
    terminal_state = None
    terminal_detail = None
    completion_token = None
    last_event_at = None
    last_prompt_value = None
    resolved_calls = set()
    latest_call = None

    for raw in reversed(lines):
        root = json_line(raw)
        if not isinstance(root, dict):
            continue
        payload = root.get("payload")
        if not isinstance(payload, dict):
            continue
        timestamp = parse_time(root.get("timestamp"))
        if timestamp is not None and last_event_at is None:
            last_event_at = timestamp
        outer_type = root.get("type")
        payload_type = payload.get("type")
        if last_prompt_value is None:
            last_prompt_value = prompt_from_record(root)
        if outer_type == "event_msg":
            if not found_boundary and payload_type == "task_started":
                unfinished = True
                found_boundary = True
            elif not found_boundary and payload_type == "task_complete":
                terminal_state = "completed"
                terminal_detail = "本轮任务已完成"
                completion_token = clean(payload.get("turn_id")) or clean(root.get("timestamp"))
                if completion_token is None and payload.get("completed_at") is not None:
                    completion_token = str(payload.get("completed_at"))
                found_boundary = True
            elif not found_boundary and payload_type == "turn_aborted":
                terminal_state = "failed"
                terminal_detail = "任务已中止"
                found_boundary = True
            elif not found_boundary and payload_type == "error":
                terminal_state = "failed"
                error = payload.get("message") or payload.get("error")
                if isinstance(error, dict):
                    error = error.get("message")
                terminal_detail = clean(error, 160) or "Codex 执行出错"
                found_boundary = True
        if found_boundary and last_prompt_value:
            break
        if found_boundary or outer_type != "response_item":
            continue
        if payload_type in ("function_call_output", "custom_tool_call_output"):
            call_id = payload.get("call_id")
            if isinstance(call_id, str):
                resolved_calls.add(call_id)
        elif payload_type in ("function_call", "custom_tool_call"):
            call_id = payload.get("call_id") or payload.get("id")
            if latest_call is None and isinstance(call_id, str) and call_id not in resolved_calls:
                latest_call = {"name": str(payload.get("name") or "tool"), "started_at": timestamp}

    updated_at = last_event_at or modified_at or now
    if not found_boundary and active_pid and latest_call:
        unfinished = True
    if unfinished:
        if latest_call and needs_attention(latest_call, approval_mode, working_child, now):
            return "attention", attention_detail(latest_call["name"]), latest_call.get("started_at") or updated_at, last_prompt_value, None
        if active_pid or now - modified_at < 12:
            detail = "正在执行命令" if working_child else "Codex 正在思考与执行"
            return "active", detail, updated_at, last_prompt_value, None
        return "failed", "会话意外停止，没有完成事件", updated_at, last_prompt_value, None
    if terminal_state:
        return terminal_state, terminal_detail, updated_at, last_prompt_value, completion_token
    if active_pid:
        return "active", "Codex 会话已启动", updated_at, last_prompt_value, None
    return "completed", "会话当前空闲", updated_at, last_prompt_value, None


def open_database(database_path):
    direct_error = None
    try:
        connection = sqlite3.connect(
            "file:{}?mode=ro".format(database_path), uri=True, timeout=1
        )
        connection.execute("PRAGMA schema_version").fetchone()
        return connection
    except sqlite3.Error as exc:
        direct_error = exc
        try:
            connection.close()
        except (NameError, sqlite3.Error):
            pass

    snapshot_root = tempfile.mkdtemp(prefix="codex-pulse-db-")
    atexit.register(shutil.rmtree, snapshot_root, ignore_errors=True)
    snapshot_path = os.path.join(snapshot_root, os.path.basename(database_path))
    try:
        shutil.copy2(database_path, snapshot_path)
        for suffix in ("-wal",):
            source = database_path + suffix
            if os.path.isfile(source):
                shutil.copy2(source, snapshot_path + suffix)
        connection = sqlite3.connect(snapshot_path, timeout=1)
        connection.execute("PRAGMA schema_version").fetchone()
        return connection
    except (OSError, sqlite3.Error) as snapshot_error:
        raise sqlite3.OperationalError(
            "direct read failed ({}); snapshot read failed ({})".format(
                direct_error, snapshot_error
            )
        ) from snapshot_error


def terminate_session(database_path, sessions_root, session_id, encoded_pid):
    try:
        expected_pid = int(decode_argument(encoded_pid))
    except ValueError as exc:
        raise ValueError("占用进程 PID 无效") from exc
    if expected_pid <= 0:
        raise ValueError("占用进程 PID 无效")

    connection = open_database(database_path)
    try:
        row = connection.execute(
            "SELECT rollout_path FROM threads WHERE id = ?", (session_id,)
        ).fetchone()
    finally:
        connection.close()
    if row is None or not row[0]:
        raise ValueError("未找到指定会话")
    rollout_path = os.path.realpath(os.path.abspath(os.path.expanduser(row[0])))
    active_paths, children, commands, parents, ttys = process_snapshot(sessions_root)
    active_pid = active_paths.get(rollout_path)
    if active_pid is None:
        print(json.dumps(
            {"ok": True, "action": "terminate", "alreadyStopped": True},
            ensure_ascii=False, separators=(",", ":")
        ))
        return
    if active_pid != expected_pid:
        raise ValueError("占用进程已经变化，请刷新后重试")
    if "codex" not in commands.get(active_pid, "").lower():
        raise ValueError("占用该会话的进程不是可确认的 Codex 进程")

    owner, tty = writer_context(active_pid, parents, commands, ttys)
    os.kill(active_pid, signal.SIGTERM)
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        try:
            os.kill(active_pid, 0)
        except ProcessLookupError:
            break
        except PermissionError as exc:
            raise ValueError("没有权限结束远程 Codex 进程") from exc
        time.sleep(0.1)
    else:
        raise ValueError("原 Codex 进程未在超时内退出，请回到原终端手动结束")

    remaining_paths, _, _, _, _ = process_snapshot(sessions_root)
    if rollout_path in remaining_paths:
        raise ValueError("会话仍被另一个 Codex 进程占用，请刷新后重试")
    result = {"ok": True, "action": "terminate", "pid": active_pid, "owner": owner}
    if tty:
        result["tty"] = tty
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


def manage_session(database_path, sessions_root, action, encoded_id, encoded_value=""):
    session_id = clean(decode_argument(encoded_id), 200)
    if not session_id:
        raise ValueError("会话 ID 无效")
    if action == "terminate":
        terminate_session(database_path, sessions_root, session_id, encoded_value)
        return
    connection = sqlite3.connect(database_path, timeout=2)
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(threads)").fetchall()}
        if action == "rename":
            name = clean(decode_argument(encoded_value), 100)
            if not name:
                raise ValueError("会话名称不能为空")
            column = "name" if "name" in columns else "title" if "title" in columns else None
            if column is None:
                raise ValueError("当前 Codex 数据库不支持会话重命名")
            cursor = connection.execute(
                'UPDATE threads SET "{}" = ? WHERE id = ?'.format(column),
                (name, session_id),
            )
        elif action == "archive":
            if "archived" not in columns:
                raise ValueError("当前 Codex 数据库不支持删除会话")
            if "archived_at" in columns:
                cursor = connection.execute(
                    "UPDATE threads SET archived = 1, archived_at = ? WHERE id = ?",
                    (int(time.time()), session_id),
                )
            else:
                cursor = connection.execute(
                    "UPDATE threads SET archived = 1 WHERE id = ?",
                    (session_id,),
                )
        else:
            raise ValueError("远程会话操作无效")
        if cursor.rowcount != 1:
            raise ValueError("未找到指定会话")
        connection.commit()
        print(json.dumps({"ok": True, "action": action}, ensure_ascii=False, separators=(",", ":")))
    finally:
        connection.close()


def main():
    codex_home = os.path.abspath(os.path.expanduser(os.environ.get("CODEX_HOME", "~/.codex")))
    database_path = os.path.join(codex_home, "state_5.sqlite")
    sessions_root = os.path.join(codex_home, "sessions")
    if len(sys.argv) > 1:
        manage_session(
            database_path,
            sessions_root,
            sys.argv[1],
            sys.argv[2] if len(sys.argv) > 2 else "",
            sys.argv[3] if len(sys.argv) > 3 else "",
        )
        return
    active_paths, children, commands, parents, ttys = process_snapshot(sessions_root)
    connection = open_database(database_path)
    connection.row_factory = sqlite3.Row
    columns = {row[1] for row in connection.execute("PRAGMA table_info(threads)").fetchall()}

    def column(name, fallback):
        return name if name in columns else fallback

    def first_text(names, fallback):
        values = ["NULLIF({}, '')".format(name) for name in names if name in columns]
        values.append(fallback)
        return "COALESCE({})".format(", ".join(values))

    title_expression = first_text(("name", "title", "preview"), "id")
    prompt_expression = first_text(("preview", "first_user_message", "title"), "id")
    conditions = []
    if "archived" in columns:
        conditions.append("archived = 0")
    if "source" in columns:
        conditions.append("source = 'cli'")
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    query = (
        "SELECT id, rollout_path, {updated} AS updated_at, {source} AS source, {cwd} AS cwd, "
        "{title} AS title, {approval} AS approval_mode, {model} AS model, "
        "{prompt} AS fallback_prompt FROM threads{where} ORDER BY updated_at DESC LIMIT 100"
    ).format(
        updated=column("updated_at", "0"), source=column("source", "'cli'"),
        cwd=column("cwd", "''"), title=title_expression,
        approval=column("approval_mode", "'never'"), model=column("model", "''"),
        prompt=prompt_expression, where=where,
    )
    rows = connection.execute(query).fetchall()
    now = time.time()
    sessions = []
    for row in rows:
        path = os.path.abspath(os.path.expanduser(row["rollout_path"] or ""))
        if not path or not os.path.isfile(path):
            continue
        modified_at = os.path.getmtime(path)
        pid = active_paths.get(path)
        working_child = has_working_child(pid, children, commands) if pid else False
        state, detail, updated_at, prompt, completion_token = detect_state(
            path, row["approval_mode"] or "never", pid, working_child, modified_at, now
        )
        prompt = prompt or latest_prompt(path) or clean(row["fallback_prompt"]) or clean(row["title"]) or row["id"]
        cwd = row["cwd"] or ""
        item = {
            "id": row["id"],
            "title": clean(row["title"]) or row["id"],
            "lastPrompt": prompt,
            "cwd": cwd,
            "projectName": os.path.basename(cwd.rstrip(os.sep)) if cwd else "",
            "state": state,
            "detail": detail,
            "updatedAt": updated_at * 1000,
        }
        if row["model"]:
            item["model"] = row["model"]
        if pid:
            item["pid"] = pid
            owner, tty = writer_context(pid, parents, commands, ttys)
            item["writerOwner"] = owner
            if tty:
                item["writerTty"] = tty
        if completion_token:
            item["completionToken"] = completion_token
        sessions.append(item)
    connection.close()
    print(json.dumps({"sessions": sessions}, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("{}: {}".format(type(exc).__name__, exc), file=sys.stderr)
        sys.exit(1)
