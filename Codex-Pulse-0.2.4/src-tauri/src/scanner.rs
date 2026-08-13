use std::{
    collections::{HashMap, HashSet},
    fs::{self, File},
    io::{Read, Seek, SeekFrom},
    path::{Path, PathBuf},
    process::Command,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use chrono::DateTime;
use rusqlite::{Connection, OpenFlags, params};
use serde_json::Value;

use crate::{
    models::{DetectedState, ProcessInfo, Session},
    settings::now_millis,
};

const MAX_TAIL_BYTES: u64 = 512 * 1024;
const PROMPT_CHUNK_BYTES: u64 = 256 * 1024;

#[derive(Clone)]
struct StateCache {
    file_size: u64,
    modified_at: i64,
    approval_mode: String,
    process_key: String,
    result: DetectedState,
}

#[derive(Clone)]
struct PromptCache {
    file_size: u64,
    prompt: String,
}

#[derive(Debug)]
struct ThreadRow {
    id: String,
    rollout_path: String,
    updated_at: String,
    source: String,
    cwd: String,
    title: String,
    approval_mode: String,
    model: String,
    fallback_prompt: String,
}

pub struct LocalScanner {
    codex_home: PathBuf,
    state_cache: HashMap<PathBuf, StateCache>,
    prompt_cache: HashMap<PathBuf, PromptCache>,
}

impl LocalScanner {
    pub fn new(codex_home: PathBuf) -> Self {
        Self {
            codex_home,
            state_cache: HashMap::new(),
            prompt_cache: HashMap::new(),
        }
    }

    pub fn scan_sessions(&mut self) -> Result<Vec<Session>, String> {
        let database_path = self.codex_home.join("state_5.sqlite");
        let sessions_root = self.codex_home.join("sessions");
        let processes = active_session_processes(&sessions_root);
        let rows = query_thread_rows(&database_path).map_err(|error| {
            format!(
                "无法打开或读取 Codex 状态数据库：{}",
                clean_string(&error, 300)
            )
        })?;
        let mut sessions = Vec::new();
        let mut scanned_paths = HashSet::new();

        for row in rows {
            if row.id.is_empty() || row.rollout_path.is_empty() {
                continue;
            }
            let rollout_path = expand_home(&row.rollout_path);
            let metadata = match fs::metadata(&rollout_path) {
                Ok(value) if value.is_file() => value,
                _ => continue,
            };
            let rollout_path = rollout_path
                .canonicalize()
                .unwrap_or_else(|_| rollout_path.clone());
            scanned_paths.insert(rollout_path.clone());
            let modified_at = system_time_millis(metadata.modified().unwrap_or(SystemTime::now()));
            let process_info = processes.get(&rollout_path).copied();
            let process_key = process_info
                .map(|info| format!("{}:{}", info.pid, u8::from(info.has_working_child)))
                .unwrap_or_else(|| "none".into());
            let approval_mode = if row.approval_mode.is_empty() {
                "never"
            } else {
                &row.approval_mode
            };

            let cached = self.state_cache.get(&rollout_path).filter(|entry| {
                entry.file_size == metadata.len()
                    && entry.modified_at == modified_at
                    && entry.approval_mode == approval_mode
                    && entry.process_key == process_key
            });
            let state = if let Some(entry) = cached {
                entry.result.clone()
            } else {
                let result = match tail_data(&rollout_path, MAX_TAIL_BYTES) {
                    Ok(data) if !data.is_empty() => detect_state(
                        &data,
                        approval_mode,
                        process_info,
                        modified_at,
                        now_millis(),
                    ),
                    _ => DetectedState {
                        state: if process_info.is_some() {
                            "active"
                        } else {
                            "failed"
                        }
                        .into(),
                        detail: if process_info.is_some() {
                            "Codex 正在运行"
                        } else {
                            "会话记录不可读"
                        }
                        .into(),
                        updated_at: modified_at,
                        last_prompt: None,
                        completion_token: None,
                    },
                };
                if result.state == "completed" || result.state == "failed" {
                    self.state_cache.insert(
                        rollout_path.clone(),
                        StateCache {
                            file_size: metadata.len(),
                            modified_at,
                            approval_mode: approval_mode.into(),
                            process_key: process_key.clone(),
                            result: result.clone(),
                        },
                    );
                } else {
                    self.state_cache.remove(&rollout_path);
                }
                result
            };

            let cached_prompt = self.prompt_cache.get(&rollout_path).cloned();
            let mut last_prompt = state.last_prompt.clone().filter(|value| !value.is_empty());
            if last_prompt.is_none() {
                let lower_bound = cached_prompt
                    .as_ref()
                    .filter(|entry| metadata.len() >= entry.file_size)
                    .map_or(0, |entry| entry.file_size);
                if metadata.len() > lower_bound || cached_prompt.is_none() {
                    last_prompt = latest_user_prompt(&rollout_path, lower_bound);
                }
                if last_prompt.is_none() {
                    last_prompt = cached_prompt.map(|entry| entry.prompt);
                }
            }

            let title = clean_title(&row.title, &row.id);
            let last_prompt = last_prompt
                .or_else(|| clean_optional(&row.fallback_prompt, 500))
                .unwrap_or_else(|| title.clone());
            self.prompt_cache.insert(
                rollout_path.clone(),
                PromptCache {
                    file_size: metadata.len(),
                    prompt: last_prompt.clone(),
                },
            );
            let fallback_updated = parse_timestamp(&row.updated_at).unwrap_or(modified_at);
            let updated_at = if state.updated_at > 0 {
                state.updated_at
            } else {
                fallback_updated
            };
            let project_name = Path::new(&row.cwd)
                .file_name()
                .and_then(|value| value.to_str())
                .unwrap_or(&row.cwd)
                .to_string();
            let completion_key = state
                .completion_token
                .as_ref()
                .map(|token| format!("{}:{token}", row.id));
            sessions.push(Session {
                id: row.id.clone(),
                short_id: row.id.chars().take(8).collect(),
                title,
                last_prompt,
                cwd: row.cwd,
                project_name,
                source: if row.source.is_empty() {
                    "cli".into()
                } else {
                    row.source
                },
                rollout_path: rollout_path.to_string_lossy().into_owned(),
                state: state.state,
                detail: state.detail,
                updated_at,
                model: clean_optional(&row.model, 100),
                pid: process_info.map(|info| info.pid),
                writer_owner: None,
                writer_tty: None,
                completion_key,
                remote_session_id: None,
                remote_host: None,
            });
        }

        self.state_cache
            .retain(|path, _| scanned_paths.contains(path));
        self.prompt_cache
            .retain(|path, _| scanned_paths.contains(path));
        sessions.sort_by(|left, right| {
            state_priority(&left.state)
                .cmp(&state_priority(&right.state))
                .then_with(|| right.updated_at.cmp(&left.updated_at))
        });
        Ok(sessions)
    }

    pub fn rename_session(&self, session_id: &str, name: &str) -> Result<String, String> {
        let id = clean_string(session_id, 200);
        let name = clean_string(name, 100);
        if id.is_empty() || name.is_empty() {
            return Err("会话名称不能为空".into());
        }
        let database = self.codex_home.join("state_5.sqlite");
        let connection = open_write(&database)?;
        let columns = thread_columns(&connection)?;
        let column = if columns.contains("name") {
            "name"
        } else if columns.contains("title") {
            "title"
        } else {
            return Err("当前 Codex 数据库不支持会话重命名".into());
        };
        let changed = connection
            .execute(
                &format!("UPDATE threads SET \"{column}\" = ?1 WHERE id = ?2"),
                params![name, id],
            )
            .map_err(|error| error.to_string())?;
        if changed != 1 {
            return Err("未找到要重命名的会话".into());
        }
        Ok(name)
    }

    pub fn archive_session(&self, session_id: &str) -> Result<(), String> {
        let id = clean_string(session_id, 200);
        if id.is_empty() {
            return Err("会话 ID 无效".into());
        }
        let database = self.codex_home.join("state_5.sqlite");
        let connection = open_write(&database)?;
        let columns = thread_columns(&connection)?;
        if !columns.contains("archived") {
            return Err("当前 Codex 数据库不支持删除会话".into());
        }
        let sql = if columns.contains("archived_at") {
            "UPDATE threads SET archived = 1, archived_at = ?1 WHERE id = ?2"
        } else {
            "UPDATE threads SET archived = 1 WHERE id = ?2"
        };
        let changed = connection
            .execute(sql, params![now_millis() / 1000, id])
            .map_err(|error| error.to_string())?;
        if changed != 1 {
            return Err("未找到要删除的会话".into());
        }
        Ok(())
    }
}

fn open_write(path: &Path) -> Result<Connection, String> {
    let connection = Connection::open(path).map_err(|error| error.to_string())?;
    connection
        .busy_timeout(Duration::from_secs(2))
        .map_err(|error| error.to_string())?;
    Ok(connection)
}

fn query_thread_rows(path: &Path) -> Result<Vec<ThreadRow>, String> {
    match query_thread_rows_direct(path) {
        Ok(rows) => Ok(rows),
        Err(direct_error) => {
            let root = tempfile::tempdir().map_err(|error| error.to_string())?;
            let snapshot = root.path().join("state_5.sqlite");
            fs::copy(path, &snapshot).map_err(|error| error.to_string())?;
            let wal = path.with_file_name("state_5.sqlite-wal");
            if wal.exists() {
                fs::copy(&wal, snapshot.with_file_name("state_5.sqlite-wal"))
                    .map_err(|error| error.to_string())?;
            }
            query_thread_rows_direct(&snapshot)
                .map_err(|snapshot_error| format!("{direct_error}; 快照读取失败：{snapshot_error}"))
        }
    }
}

fn query_thread_rows_direct(path: &Path) -> Result<Vec<ThreadRow>, String> {
    let connection = Connection::open_with_flags(
        path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .map_err(|error| error.to_string())?;
    connection
        .busy_timeout(Duration::from_secs(1))
        .map_err(|error| error.to_string())?;
    let columns = thread_columns(&connection)?;
    let column = |name: &str, fallback: &str| {
        if columns.contains(name) {
            format!("COALESCE(\"{name}\", {fallback})")
        } else {
            fallback.into()
        }
    };
    let first_text = |names: &[&str], fallback: &str| {
        let mut values: Vec<String> = names
            .iter()
            .filter(|name| columns.contains(**name))
            .map(|name| format!("NULLIF(\"{name}\", '')"))
            .collect();
        values.push(fallback.into());
        format!("COALESCE({})", values.join(", "))
    };
    let mut conditions = Vec::new();
    if columns.contains("archived") {
        conditions.push("\"archived\" = 0");
    }
    if columns.contains("source") {
        conditions.push("\"source\" = 'cli'");
    }
    let where_clause = if conditions.is_empty() {
        String::new()
    } else {
        format!(" WHERE {}", conditions.join(" AND "))
    };
    let sql = format!(
        "SELECT CAST(COALESCE(\"id\", '') AS TEXT), \
         CAST(COALESCE(\"rollout_path\", '') AS TEXT), \
         CAST({} AS TEXT), CAST({} AS TEXT), CAST({} AS TEXT), CAST({} AS TEXT), \
         CAST({} AS TEXT), CAST({} AS TEXT), CAST({} AS TEXT) \
         FROM threads{} ORDER BY {} DESC LIMIT 100",
        column("updated_at", "0"),
        column("source", "'cli'"),
        column("cwd", "''"),
        first_text(&["name", "title", "preview"], "\"id\""),
        column("approval_mode", "'never'"),
        column("model", "''"),
        first_text(&["preview", "first_user_message", "title"], "\"id\""),
        where_clause,
        column("updated_at", "0")
    );
    let mut statement = connection
        .prepare(&sql)
        .map_err(|error| error.to_string())?;
    let rows = statement
        .query_map([], |row| {
            Ok(ThreadRow {
                id: row.get(0)?,
                rollout_path: row.get(1)?,
                updated_at: row.get(2)?,
                source: row.get(3)?,
                cwd: row.get(4)?,
                title: row.get(5)?,
                approval_mode: row.get(6)?,
                model: row.get(7)?,
                fallback_prompt: row.get(8)?,
            })
        })
        .map_err(|error| error.to_string())?;
    rows.collect::<Result<Vec<_>, _>>()
        .map_err(|error| error.to_string())
}

fn thread_columns(connection: &Connection) -> Result<HashSet<String>, String> {
    let mut statement = connection
        .prepare("PRAGMA table_info(threads)")
        .map_err(|error| error.to_string())?;
    let columns = statement
        .query_map([], |row| row.get::<_, String>(1))
        .map_err(|error| error.to_string())?
        .collect::<Result<HashSet<_>, _>>()
        .map_err(|error| error.to_string())?;
    if columns.is_empty() {
        Err("Codex 状态数据库中缺少 threads 表".into())
    } else {
        Ok(columns)
    }
}

fn active_session_processes(sessions_root: &Path) -> HashMap<PathBuf, ProcessInfo> {
    let lsof = Command::new("/usr/sbin/lsof")
        .args(["-F", "pn", "+D"])
        .arg(sessions_root)
        .output()
        .map(|output| String::from_utf8_lossy(&output.stdout).into_owned())
        .unwrap_or_default();
    let ps = Command::new("/bin/ps")
        .args(["-axo", "pid=,ppid=,comm="])
        .output()
        .map(|output| String::from_utf8_lossy(&output.stdout).into_owned())
        .unwrap_or_default();
    let mut path_pids = HashMap::new();
    let mut current_pid = None;
    for line in lsof.lines() {
        if let Some(value) = line.strip_prefix('p') {
            current_pid = value.parse::<u32>().ok();
        } else if let Some(value) = line.strip_prefix('n')
            && value.ends_with(".jsonl")
            && let Some(pid) = current_pid
        {
            let path = PathBuf::from(value)
                .canonicalize()
                .unwrap_or_else(|_| PathBuf::from(value));
            path_pids.insert(path, pid);
        }
    }
    let mut tree: HashMap<u32, Vec<(u32, String)>> = HashMap::new();
    for line in ps.lines() {
        let mut fields = line.split_whitespace();
        let Some(pid) = fields.next().and_then(|value| value.parse::<u32>().ok()) else {
            continue;
        };
        let Some(parent) = fields.next().and_then(|value| value.parse::<u32>().ok()) else {
            continue;
        };
        tree.entry(parent)
            .or_default()
            .push((pid, fields.collect::<Vec<_>>().join(" ")));
    }
    path_pids
        .into_iter()
        .map(|(path, pid)| {
            (
                path,
                ProcessInfo {
                    pid,
                    has_working_child: has_working_descendant(pid, &tree),
                },
            )
        })
        .collect()
}

fn has_working_descendant(root: u32, tree: &HashMap<u32, Vec<(u32, String)>>) -> bool {
    let mut queue = tree.get(&root).cloned().unwrap_or_default();
    let mut visited = HashSet::new();
    while let Some((pid, command)) = queue.pop() {
        if !visited.insert(pid) {
            continue;
        }
        let lower = command.to_lowercase();
        let helper = lower.contains("codex-code-mode-host")
            || lower.ends_with("/codex")
            || lower.ends_with("/node")
            || lower == "codex"
            || lower == "node";
        if !helper {
            return true;
        }
        queue.extend(tree.get(&pid).cloned().unwrap_or_default());
    }
    false
}

fn tail_data(path: &Path, maximum: u64) -> std::io::Result<Vec<u8>> {
    let mut file = File::open(path)?;
    let size = file.metadata()?.len();
    let start = size.saturating_sub(maximum);
    file.seek(SeekFrom::Start(start))?;
    let mut data = Vec::with_capacity((size - start) as usize);
    file.read_to_end(&mut data)?;
    if start == 0 {
        return Ok(data);
    }
    Ok(data
        .iter()
        .position(|byte| *byte == b'\n')
        .map_or_else(Vec::new, |position| data[position + 1..].to_vec()))
}

fn latest_user_prompt(path: &Path, lower_bound: u64) -> Option<String> {
    let mut file = File::open(path).ok()?;
    let size = file.metadata().ok()?.len();
    let boundary = if lower_bound > size { 0 } else { lower_bound };
    let mut position = size;
    let mut carry = Vec::new();
    while position > boundary {
        let start = boundary.max(position.saturating_sub(PROMPT_CHUNK_BYTES));
        file.seek(SeekFrom::Start(start)).ok()?;
        let mut chunk = vec![0; (position - start) as usize];
        file.read_exact(&mut chunk).ok()?;
        chunk.extend_from_slice(&carry);
        let mut end = chunk.len();
        while let Some(newline) = chunk[..end].iter().rposition(|byte| *byte == b'\n') {
            if end > newline + 1
                && let Ok(record) = serde_json::from_slice::<Value>(&chunk[newline + 1..end])
                && let Some(prompt) = prompt_from_record(&record)
            {
                return Some(prompt);
            }
            end = newline;
        }
        carry = chunk[..end].to_vec();
        position = start;
    }
    serde_json::from_slice::<Value>(&carry)
        .ok()
        .and_then(|record| prompt_from_record(&record))
}

pub fn detect_state(
    data: &[u8],
    approval_mode: &str,
    process_info: Option<ProcessInfo>,
    file_modified_at: i64,
    now: i64,
) -> DetectedState {
    let text = String::from_utf8_lossy(data);
    if text.is_empty() {
        return DetectedState {
            state: if process_info.is_some() {
                "active"
            } else {
                "failed"
            }
            .into(),
            detail: if process_info.is_some() {
                "Codex 正在运行"
            } else {
                "会话记录不可读"
            }
            .into(),
            updated_at: file_modified_at,
            last_prompt: None,
            completion_token: None,
        };
    }
    let mut unfinished = false;
    let mut found_boundary = false;
    let mut terminal_state = None;
    let mut terminal_detail = None;
    let mut completion_token = None;
    let mut last_event_at = None;
    let mut latest_call: Option<(String, Option<i64>)> = None;
    let mut last_prompt = None;
    let mut resolved_calls = HashSet::new();

    for line in text.lines().rev().filter(|line| !line.is_empty()) {
        let Ok(root) = serde_json::from_str::<Value>(line) else {
            continue;
        };
        let Some(payload) = root.get("payload").and_then(Value::as_object) else {
            continue;
        };
        let timestamp = root.get("timestamp").and_then(parse_json_timestamp);
        if last_event_at.is_none() {
            last_event_at = timestamp;
        }
        if last_prompt.is_none() {
            last_prompt = prompt_from_record(&root);
        }
        let outer_type = root.get("type").and_then(Value::as_str).unwrap_or_default();
        let payload_type = payload
            .get("type")
            .and_then(Value::as_str)
            .unwrap_or_default();
        if outer_type == "event_msg" {
            if !found_boundary && payload_type == "task_started" {
                unfinished = true;
                found_boundary = true;
            } else if !found_boundary && payload_type == "task_complete" {
                terminal_state = Some("completed".to_string());
                terminal_detail = Some("本轮任务已完成".to_string());
                completion_token = payload
                    .get("turn_id")
                    .and_then(Value::as_str)
                    .and_then(|value| clean_optional(value, 500))
                    .or_else(|| {
                        root.get("timestamp")
                            .and_then(Value::as_str)
                            .map(str::to_string)
                    })
                    .or_else(|| payload.get("completed_at").map(Value::to_string));
                found_boundary = true;
            } else if !found_boundary && payload_type == "turn_aborted" {
                terminal_state = Some("failed".to_string());
                terminal_detail = Some("任务已中止".to_string());
                found_boundary = true;
            } else if !found_boundary && payload_type == "error" {
                terminal_state = Some("failed".to_string());
                let message = payload
                    .get("error")
                    .and_then(|value| value.get("message").or(Some(value)))
                    .and_then(Value::as_str)
                    .or_else(|| payload.get("message").and_then(Value::as_str))
                    .unwrap_or("Codex 执行出错");
                terminal_detail = Some(clean_string(message, 160));
                found_boundary = true;
            }
        }
        if found_boundary && last_prompt.is_some() {
            break;
        }
        if found_boundary || outer_type != "response_item" {
            continue;
        }
        if payload_type == "function_call_output" || payload_type == "custom_tool_call_output" {
            if let Some(call_id) = payload.get("call_id").and_then(Value::as_str) {
                resolved_calls.insert(call_id.to_string());
            }
        } else if payload_type == "function_call" || payload_type == "custom_tool_call" {
            let call_id = payload
                .get("call_id")
                .or_else(|| payload.get("id"))
                .and_then(Value::as_str);
            if latest_call.is_none() && call_id.is_some_and(|id| !resolved_calls.contains(id)) {
                latest_call = Some((
                    payload
                        .get("name")
                        .and_then(Value::as_str)
                        .unwrap_or("tool")
                        .to_string(),
                    timestamp,
                ));
            }
        }
    }

    let updated_at = last_event_at.unwrap_or(file_modified_at.max(now));
    if !found_boundary && process_info.is_some() && latest_call.is_some() {
        unfinished = true;
    }
    if unfinished {
        let working = process_info.is_some_and(|info| info.has_working_child);
        if let Some((name, started_at)) = &latest_call
            && needs_attention(name, *started_at, approval_mode, working, now)
        {
            return DetectedState {
                state: "attention".into(),
                detail: attention_detail(name).into(),
                updated_at: started_at.unwrap_or(updated_at),
                last_prompt,
                completion_token: None,
            };
        }
        if process_info.is_some() || now - file_modified_at < 12_000 {
            return DetectedState {
                state: "active".into(),
                detail: if working {
                    "正在执行命令"
                } else {
                    "Codex 正在思考与执行"
                }
                .into(),
                updated_at,
                last_prompt,
                completion_token: None,
            };
        }
        return DetectedState {
            state: "failed".into(),
            detail: "会话意外停止，没有完成事件".into(),
            updated_at,
            last_prompt,
            completion_token: None,
        };
    }
    if let Some(state) = terminal_state {
        return DetectedState {
            state,
            detail: terminal_detail.unwrap_or_default(),
            updated_at,
            last_prompt,
            completion_token,
        };
    }
    DetectedState {
        state: if process_info.is_some() {
            "active"
        } else {
            "completed"
        }
        .into(),
        detail: if process_info.is_some() {
            "Codex 会话已启动"
        } else {
            "会话当前空闲"
        }
        .into(),
        updated_at,
        last_prompt,
        completion_token: None,
    }
}

fn needs_attention(
    name: &str,
    started_at: Option<i64>,
    approval_mode: &str,
    working: bool,
    now: i64,
) -> bool {
    let lower = name.to_lowercase();
    if started_at.is_some_and(|value| now - value < 1_200) {
        return false;
    }
    if lower.contains("request_user_input") || lower.contains("requestpermission") {
        return true;
    }
    if approval_mode.eq_ignore_ascii_case("never") || working {
        return false;
    }
    ["exec", "shell", "apply_patch", "write", "permission", "mcp"]
        .iter()
        .any(|needle| lower.contains(needle))
}

fn attention_detail(name: &str) -> &'static str {
    let lower = name.to_lowercase();
    if lower.contains("request_user_input") {
        "Codex 正在等待你的选择"
    } else if lower.contains("permission") {
        "Codex 正在请求权限"
    } else if lower.contains("apply_patch") || lower.contains("write") {
        "文件修改等待确认"
    } else if lower.contains("mcp") {
        "外部工具调用等待确认"
    } else {
        "命令执行等待确认"
    }
}

fn prompt_from_record(root: &Value) -> Option<String> {
    let payload = root.get("payload")?;
    if root.get("type").and_then(Value::as_str) == Some("event_msg")
        && payload.get("type").and_then(Value::as_str) == Some("user_message")
    {
        return payload
            .get("message")
            .and_then(Value::as_str)
            .and_then(|value| clean_optional(value, 500));
    }
    if root.get("type").and_then(Value::as_str) != Some("response_item")
        || payload.get("type").and_then(Value::as_str) != Some("message")
        || payload.get("role").and_then(Value::as_str) != Some("user")
    {
        return None;
    }
    if let Some(content) = payload.get("content").and_then(Value::as_str) {
        return clean_optional(content, 500);
    }
    let text = payload
        .get("content")
        .and_then(Value::as_array)?
        .iter()
        .filter(|item| {
            matches!(
                item.get("type").and_then(Value::as_str),
                Some("input_text" | "text")
            )
        })
        .filter_map(|item| item.get("text").and_then(Value::as_str))
        .collect::<Vec<_>>()
        .join(" ");
    clean_optional(&text, 500)
}

fn parse_json_timestamp(value: &Value) -> Option<i64> {
    if let Some(number) = value.as_f64() {
        return Some(if number > 1e12 {
            number as i64
        } else {
            (number * 1000.0) as i64
        });
    }
    value.as_str().and_then(parse_timestamp)
}

fn parse_timestamp(value: &str) -> Option<i64> {
    if let Ok(number) = value.parse::<f64>() {
        return Some(if number > 1e12 {
            number as i64
        } else {
            (number * 1000.0) as i64
        });
    }
    DateTime::parse_from_rfc3339(value)
        .ok()
        .map(|date| date.timestamp_millis())
}

fn clean_title(value: &str, fallback_id: &str) -> String {
    let clean = clean_string(value, 100);
    if clean.is_empty() || clean == fallback_id {
        format!(
            "Codex 会话 {}",
            fallback_id.chars().take(8).collect::<String>()
        )
    } else {
        clean
    }
}

pub fn clean_string(value: &str, limit: usize) -> String {
    let clean = value.split_whitespace().collect::<Vec<_>>().join(" ");
    if clean.chars().count() <= limit {
        clean
    } else {
        format!("{}…", clean.chars().take(limit).collect::<String>())
    }
}

fn clean_optional(value: &str, limit: usize) -> Option<String> {
    let clean = clean_string(value, limit);
    (!clean.is_empty()).then_some(clean)
}

fn expand_home(value: &str) -> PathBuf {
    if value == "~" {
        return dirs::home_dir().unwrap_or_else(|| PathBuf::from(value));
    }
    if let Some(rest) = value.strip_prefix("~/") {
        return dirs::home_dir()
            .unwrap_or_else(|| PathBuf::from("~"))
            .join(rest);
    }
    PathBuf::from(value)
}

fn system_time_millis(value: SystemTime) -> i64 {
    value
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as i64
}

fn state_priority(state: &str) -> u8 {
    match state {
        "active" => 0,
        "completed" => 1,
        "attention" => 2,
        "failed" => 3,
        _ => 99,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const NOW: i64 = 2_000_000_000_000;

    fn record(outer: &str, payload: Value, seconds_ago: i64) -> String {
        serde_json::json!({
            "timestamp": chrono::DateTime::from_timestamp_millis(NOW - seconds_ago * 1000).unwrap().to_rfc3339(),
            "type": outer,
            "payload": payload
        })
        .to_string()
    }

    fn event(name: &str, seconds: i64) -> String {
        record("event_msg", serde_json::json!({"type": name}), seconds)
    }

    fn detect(
        lines: Vec<String>,
        approval: &str,
        process: Option<ProcessInfo>,
        modified: i64,
    ) -> String {
        detect_state(
            lines.join("\n").as_bytes(),
            approval,
            process,
            NOW - modified * 1000,
            NOW,
        )
        .state
    }

    #[test]
    fn detects_turn_states() {
        let idle = Some(ProcessInfo {
            pid: 7,
            has_working_child: false,
        });
        let working = Some(ProcessInfo {
            pid: 7,
            has_working_child: true,
        });
        assert_eq!(
            detect(vec![event("task_started", 5)], "never", idle, 5),
            "active"
        );
        assert_eq!(
            detect(
                vec![event("task_started", 10), event("task_complete", 1)],
                "on-request",
                idle,
                1
            ),
            "completed"
        );
        let call = record(
            "response_item",
            serde_json::json!({"type":"function_call","call_id":"call-1","name":"exec_command"}),
            5,
        );
        assert_eq!(
            detect(
                vec![event("task_started", 10), call.clone()],
                "on-request",
                idle,
                5
            ),
            "attention"
        );
        assert_eq!(
            detect(
                vec![event("task_started", 10), call],
                "on-request",
                working,
                5
            ),
            "active"
        );
        assert_eq!(
            detect(vec![event("task_started", 30)], "never", None, 30),
            "failed"
        );
    }

    #[test]
    fn extracts_latest_prompt_and_completion() {
        let input = record(
            "event_msg",
            serde_json::json!({"type":"user_message","message":" 第一行\n第二行 "}),
            9,
        );
        let complete = record(
            "event_msg",
            serde_json::json!({"type":"task_complete","turn_id":"turn-123"}),
            1,
        );
        let result = detect_state(
            [event("task_started", 10), input, complete]
                .join("\n")
                .as_bytes(),
            "never",
            Some(ProcessInfo {
                pid: 7,
                has_working_child: false,
            }),
            NOW - 1000,
            NOW,
        );
        assert_eq!(result.last_prompt.as_deref(), Some("第一行 第二行"));
        assert_eq!(result.completion_token.as_deref(), Some("turn-123"));
    }
}
