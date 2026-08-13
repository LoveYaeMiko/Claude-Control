use std::{
    collections::HashSet,
    fs,
    io::{Read, Write},
    path::{Path, PathBuf},
    process::{Command, Stdio},
    thread,
    time::Duration,
};

use base64::{Engine as _, engine::general_purpose::URL_SAFE_NO_PAD};
use glob::glob;
use serde_json::Value;
use wait_timeout::ChildExt;

use crate::{models::Session, scanner::clean_string, settings::now_millis};

const REMOTE_SCRIPT: &str = include_str!("../../src/remote/remote_scanner.py");
const MAX_REMOTE_OUTPUT: usize = 16 * 1024 * 1024;

pub fn is_valid_host(host: &str) -> bool {
    let mut characters = host.chars();
    let Some(first) = characters.next() else {
        return false;
    };
    host.len() <= 255
        && first.is_ascii_alphanumeric()
        && characters.all(|value| {
            value.is_ascii_alphanumeric() || matches!(value, '.' | '_' | ':' | '@' | '-')
        })
}

pub fn scan_host(host: &str) -> Result<Vec<Session>, String> {
    let output = run_script(host, &[])?;
    sessions_from_json(&output, host)
}

pub fn manage_session(
    host: &str,
    action: &str,
    session_id: &str,
    value: &str,
) -> Result<(), String> {
    if !matches!(action, "rename" | "archive" | "terminate") {
        return Err("远程会话操作无效".into());
    }
    let session_id = clean_string(session_id, 200);
    let value = clean_string(value, 100);
    if session_id.is_empty() {
        return Err("远程会话 ID 无效".into());
    }
    if action == "rename" && value.is_empty() {
        return Err("会话名称不能为空".into());
    }
    if action == "terminate" && value.parse::<u32>().ok().filter(|pid| *pid > 0).is_none() {
        return Err("占用进程 PID 无效".into());
    }
    let arguments = [
        action.to_string(),
        URL_SAFE_NO_PAD.encode(session_id),
        URL_SAFE_NO_PAD.encode(value),
    ];
    let output = run_script(host, &arguments)?;
    let root = parse_json_result(&output).ok_or("远程服务器返回了无法识别的数据")?;
    if root.get("ok").and_then(Value::as_bool) == Some(true) {
        Ok(())
    } else {
        Err(root
            .get("error")
            .and_then(Value::as_str)
            .map(|message| clean_string(message, 500))
            .filter(|message| !message.is_empty())
            .unwrap_or_else(|| "远程会话操作失败".into()))
    }
}

fn run_script(host: &str, arguments: &[String]) -> Result<Vec<u8>, String> {
    if !is_valid_host(host) {
        return Err("SSH 主机名格式无效".into());
    }
    let mut command = Command::new("/usr/bin/ssh");
    command.args([
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=8",
        "-o",
        "ServerAliveInterval=5",
        "-o",
        "ServerAliveCountMax=1",
        host,
        "python3",
        "-",
    ]);
    command.args(arguments);
    let mut child = command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|error| format!("无法启动 SSH：{error}"))?;

    if let Some(mut stdin) = child.stdin.take() {
        stdin
            .write_all(REMOTE_SCRIPT.as_bytes())
            .map_err(|error| format!("无法发送远程扫描脚本：{error}"))?;
    }

    let stdout = child.stdout.take().ok_or("无法读取 SSH 输出")?;
    let stderr = child.stderr.take().ok_or("无法读取 SSH 错误输出")?;
    let stdout_reader = thread::spawn(move || read_limited(stdout, MAX_REMOTE_OUTPUT + 1));
    let stderr_reader = thread::spawn(move || read_limited(stderr, 256 * 1024));

    let status = match child
        .wait_timeout(Duration::from_secs(25))
        .map_err(|error| format!("等待 SSH 失败：{error}"))?
    {
        Some(status) => status,
        None => {
            let _ = child.kill();
            let _ = child.wait();
            let _ = stdout_reader.join();
            let _ = stderr_reader.join();
            return Err("连接远程服务器超时".into());
        }
    };
    let stdout = stdout_reader
        .join()
        .map_err(|_| "读取 SSH 输出失败".to_string())??;
    let stderr = stderr_reader
        .join()
        .map_err(|_| "读取 SSH 错误输出失败".to_string())??;
    if stdout.len() > MAX_REMOTE_OUTPUT {
        return Err("远程服务器返回的数据过大".into());
    }
    if !status.success() {
        let message = clean_string(&String::from_utf8_lossy(&stderr), 500);
        let fallback = clean_string(&String::from_utf8_lossy(&stdout), 500);
        return Err(if !message.is_empty() {
            message
        } else if !fallback.is_empty() {
            fallback
        } else {
            "无法连接远程服务器".into()
        });
    }
    Ok(stdout)
}

fn read_limited(reader: impl Read, limit: usize) -> Result<Vec<u8>, String> {
    let mut output = Vec::new();
    reader
        .take(limit as u64)
        .read_to_end(&mut output)
        .map_err(|error| error.to_string())?;
    Ok(output)
}

fn sessions_from_json(data: &[u8], host: &str) -> Result<Vec<Session>, String> {
    let root = parse_json_result(data).ok_or("远程服务器返回了无法识别的数据")?;
    let items = root
        .as_array()
        .or_else(|| root.get("sessions").and_then(Value::as_array))
        .ok_or("远程服务器返回了无法识别的数据")?;
    let mut sessions = Vec::new();
    for item in items {
        let Some(remote_id) = item
            .get("id")
            .and_then(Value::as_str)
            .map(|value| clean_string(value, 200))
            .filter(|value| !value.is_empty())
        else {
            continue;
        };
        let cwd = json_text(item, "cwd", 500).unwrap_or_default();
        let short_id: String = remote_id.chars().take(8).collect();
        let title = json_text(item, "title", 100);
        let last_prompt = json_text(item, "lastPrompt", 500)
            .or_else(|| title.clone())
            .unwrap_or_else(|| format!("Codex 会话 {short_id}"));
        let state = json_text(item, "state", 40).unwrap_or_else(|| "completed".into());
        let completion_token = json_text(item, "completionToken", 500);
        sessions.push(Session {
            id: format!("remote:{host}:{remote_id}"),
            short_id,
            title: title.unwrap_or_else(|| last_prompt.clone()),
            last_prompt,
            project_name: json_text(item, "projectName", 200).unwrap_or_else(|| {
                Path::new(&cwd)
                    .file_name()
                    .and_then(|value| value.to_str())
                    .unwrap_or("远程目录")
                    .to_string()
            }),
            cwd,
            source: "remote".into(),
            rollout_path: String::new(),
            detail: json_text(item, "detail", 500).unwrap_or_else(|| "远程会话".into()),
            updated_at: item
                .get("updatedAt")
                .and_then(Value::as_f64)
                .map(|value| value as i64)
                .unwrap_or_else(now_millis),
            model: json_text(item, "model", 100),
            pid: item
                .get("pid")
                .and_then(Value::as_u64)
                .filter(|pid| *pid > 0 && *pid <= u32::MAX as u64)
                .map(|pid| pid as u32),
            writer_owner: json_text(item, "writerOwner", 80),
            writer_tty: json_text(item, "writerTty", 40),
            completion_key: completion_token
                .filter(|_| state == "completed")
                .map(|token| format!("remote:{host}:{remote_id}:{token}")),
            remote_session_id: Some(remote_id),
            remote_host: Some(host.into()),
            state,
        });
    }
    Ok(sessions)
}

fn json_text(value: &Value, key: &str, limit: usize) -> Option<String> {
    value
        .get(key)
        .and_then(Value::as_str)
        .map(|value| clean_string(value, limit))
        .filter(|value| !value.is_empty())
}

fn parse_json_result(data: &[u8]) -> Option<Value> {
    serde_json::from_slice(data).ok().or_else(|| {
        data.split(|byte| *byte == b'\n')
            .rev()
            .find_map(|line| serde_json::from_slice(line).ok())
    })
}

pub fn discover_ssh_hosts() -> Vec<String> {
    let path = dirs::home_dir()
        .unwrap_or_else(|| PathBuf::from("."))
        .join(".ssh/config");
    hosts_from_ssh_config(&path)
}

pub fn hosts_from_ssh_config(path: &Path) -> Vec<String> {
    let mut hosts = Vec::new();
    collect_ssh_hosts(
        path,
        &mut hosts,
        &mut HashSet::new(),
        &mut HashSet::new(),
        0,
    );
    hosts
}

fn collect_ssh_hosts(
    path: &Path,
    hosts: &mut Vec<String>,
    host_keys: &mut HashSet<String>,
    visited: &mut HashSet<PathBuf>,
    depth: u8,
) {
    if depth > 12 {
        return;
    }
    let canonical = path.canonicalize().unwrap_or_else(|_| path.to_path_buf());
    if !visited.insert(canonical.clone()) {
        return;
    }
    let Ok(contents) = fs::read_to_string(&canonical) else {
        return;
    };
    let base = canonical.parent().unwrap_or_else(|| Path::new("."));
    for line in contents.lines() {
        let mut tokens = tokenize_ssh_config_line(line);
        if tokens.is_empty() {
            continue;
        }
        let mut keyword = tokens.remove(0);
        if let Some((name, first_value)) = keyword.split_once('=') {
            let name = name.to_string();
            if !first_value.is_empty() {
                tokens.insert(0, first_value.to_string());
            }
            keyword = name;
        }
        if keyword.eq_ignore_ascii_case("host") {
            for host in tokens {
                let key = host.to_ascii_lowercase();
                if is_valid_host(&host) && host_keys.insert(key) {
                    hosts.push(host);
                }
            }
        } else if keyword.eq_ignore_ascii_case("include") {
            for value in tokens {
                let expanded = expand_home(&value);
                let pattern = if expanded.is_absolute() {
                    expanded
                } else {
                    base.join(expanded)
                };
                let Some(pattern) = pattern.to_str() else {
                    continue;
                };
                if let Ok(matches) = glob(pattern) {
                    for included in matches.flatten().filter(|entry| entry.is_file()) {
                        collect_ssh_hosts(&included, hosts, host_keys, visited, depth + 1);
                    }
                }
            }
        }
    }
}

pub fn tokenize_ssh_config_line(line: &str) -> Vec<String> {
    let mut tokens = Vec::new();
    let mut current = String::new();
    let mut single_quoted = false;
    let mut double_quoted = false;
    let mut escaped = false;
    for character in line.chars() {
        if escaped {
            current.push(character);
            escaped = false;
        } else if character == '\\' && !single_quoted {
            escaped = true;
        } else if character == '\'' && !double_quoted {
            single_quoted = !single_quoted;
        } else if character == '"' && !single_quoted {
            double_quoted = !double_quoted;
        } else if character == '#' && !single_quoted && !double_quoted {
            break;
        } else if character.is_whitespace() && !single_quoted && !double_quoted {
            if !current.is_empty() {
                tokens.push(std::mem::take(&mut current));
            }
        } else {
            current.push(character);
        }
    }
    if escaped {
        current.push('\\');
    }
    if !current.is_empty() {
        tokens.push(current);
    }
    tokens
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn validates_hosts_and_tokenizes_config() {
        assert!(is_valid_host("user@dev-box"));
        assert!(!is_valid_host("-oProxyCommand=x"));
        assert_eq!(
            tokenize_ssh_config_line("Host dev-box \"quoted host\" # ignored"),
            vec!["Host", "dev-box", "quoted host"]
        );
    }

    #[test]
    fn discovers_included_hosts() {
        let root = tempfile::tempdir().unwrap();
        let included = root.path().join("hosts.conf");
        fs::write(&included, "Host dev-box\n").unwrap();
        let config = root.path().join("config");
        fs::write(
            &config,
            format!("Include {}\nHost *.invalid local\n", included.display()),
        )
        .unwrap();
        assert_eq!(hosts_from_ssh_config(&config), vec!["dev-box", "local"]);
    }

    #[test]
    fn normalizes_remote_sessions() {
        let data = r#"{"sessions":[{"id":"remote-id","title":"标题","lastPrompt":"最后提问","cwd":"/srv/demo","state":"completed","updatedAt":42,"completionToken":"turn-1"}]}"#;
        let sessions = sessions_from_json(data.as_bytes(), "dev-box").unwrap();
        assert_eq!(sessions.len(), 1);
        assert_eq!(sessions[0].id, "remote:dev-box:remote-id");
        assert_eq!(sessions[0].project_name, "demo");
        assert_eq!(
            sessions[0].completion_key.as_deref(),
            Some("remote:dev-box:remote-id:turn-1")
        );
    }
}
