use std::process::{Command, Stdio};

use crate::models::Session;

pub fn shell_quote(value: &str) -> String {
    format!("'{}'", value.replace('\'', "'\\''"))
}

fn apple_script_string(value: &str) -> String {
    value.replace('\\', "\\\\").replace('"', "\\\"")
}

pub fn resume_command(session: &Session, yolo_enabled: bool) -> String {
    let mode = if yolo_enabled {
        " --dangerously-bypass-approvals-and-sandbox"
    } else {
        ""
    };
    format!(
        "cd {} && codex resume{} {}",
        shell_quote(&session.cwd),
        mode,
        shell_quote(&session.id)
    )
}

pub fn remote_resume_command(session: &Session, yolo_enabled: bool) -> String {
    let mode = if yolo_enabled {
        " --dangerously-bypass-approvals-and-sandbox"
    } else {
        ""
    };
    let codex_path = "PATH=\"/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$HOME/.codex/packages/standalone/current:$PATH\"";
    let remote_id = session.remote_session_id.as_deref().unwrap_or(&session.id);
    let remote = format!(
        "cd {} && {} codex resume{} {}",
        shell_quote(&session.cwd),
        codex_path,
        mode,
        shell_quote(remote_id)
    );
    format!(
        "ssh -t {} {}",
        shell_quote(session.remote_host.as_deref().unwrap_or_default()),
        shell_quote(&remote)
    )
}

pub fn launch_terminal(command: &str) -> Result<(), String> {
    let script = format!(
        "tell application \"Terminal\"\nactivate\ndo script \"{}\"\nend tell",
        apple_script_string(command)
    );
    Command::new("/usr/bin/osascript")
        .args(["-e", &script])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map(|_| ())
        .map_err(|error| format!("无法打开终端：{error}"))
}

pub fn reveal_in_finder(path: &str) -> Result<(), String> {
    Command::new("/usr/bin/open")
        .args(["-R", path])
        .spawn()
        .map(|_| ())
        .map_err(|error| format!("无法在 Finder 中显示：{error}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture() -> Session {
        Session {
            id: "abc-123".into(),
            short_id: "abc-123".into(),
            title: "demo".into(),
            last_prompt: "demo".into(),
            cwd: "/tmp/demo folder".into(),
            project_name: "demo folder".into(),
            source: "cli".into(),
            rollout_path: String::new(),
            state: "completed".into(),
            detail: String::new(),
            updated_at: 0,
            model: None,
            pid: None,
            writer_owner: None,
            writer_tty: None,
            completion_key: None,
            remote_session_id: None,
            remote_host: None,
        }
    }

    #[test]
    fn builds_safe_resume_commands() {
        let local = fixture();
        assert_eq!(
            resume_command(&local, false),
            "cd '/tmp/demo folder' && codex resume 'abc-123'"
        );
        let mut remote = fixture();
        remote.source = "remote".into();
        remote.remote_host = Some("dev-box".into());
        remote.remote_session_id = Some("remote-123".into());
        assert!(remote_resume_command(&remote, true).contains("--dangerously-bypass"));
    }
}
