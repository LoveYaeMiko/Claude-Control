use std::{
    fs, io,
    path::{Path, PathBuf},
    time::{SystemTime, UNIX_EPOCH},
};

use crate::models::Settings;

pub fn now_millis() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as i64
}

pub fn default_settings_path() -> PathBuf {
    #[cfg(target_os = "macos")]
    {
        dirs::home_dir()
            .unwrap_or_else(|| PathBuf::from("."))
            .join("Library/Application Support/Codex Pulse/settings.json")
    }
    #[cfg(not(target_os = "macos"))]
    {
        dirs::config_dir()
            .unwrap_or_else(|| PathBuf::from("."))
            .join("Codex Pulse/settings.json")
    }
}

pub fn load(path: &Path) -> Settings {
    let parsed = fs::read_to_string(path)
        .ok()
        .and_then(|data| serde_json::from_str::<Settings>(&data).ok())
        .unwrap_or_default();
    let normalized = parsed.normalize(now_millis());
    let _ = save(path, &normalized);
    normalized
}

pub fn save(path: &Path, settings: &Settings) -> io::Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let temporary = path.with_extension("json.tmp");
    let data =
        serde_json::to_vec_pretty(settings).map_err(|error| io::Error::other(error.to_string()))?;
    fs::write(&temporary, data)?;
    fs::rename(temporary, path)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn preserves_and_normalizes_settings() {
        let root = tempfile::tempdir().unwrap();
        let path = root.path().join("settings.json");
        fs::write(
            &path,
            r#"{
              "windowPinned": true,
              "sessionTitleMode": "unexpected",
              "displayLimits": {"active": 0, "completedPending": 9, "failed": 2},
              "titleLines": 2
            }"#,
        )
        .unwrap();
        let settings = load(&path);
        assert!(settings.window_pinned);
        assert_eq!(settings.session_title_mode, "prompt");
        assert_eq!(settings.display_limits.active, 4);
        assert_eq!(settings.display_limits.completed_pending, 3);
        assert_eq!(settings.display_limits.failed, 2);
        assert_eq!(settings.title_lines, 2);
        assert!(settings.completion_tracking_started_at > 0);
    }
}
