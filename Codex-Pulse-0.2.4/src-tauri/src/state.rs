use std::{
    collections::{BTreeMap, HashSet},
    path::PathBuf,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    thread,
    time::Duration,
};

use tauri::{AppHandle, Emitter};

use crate::{
    models::{DisplayLimits, PublishedState, Session, Settings},
    remote,
    scanner::{LocalScanner, clean_string},
    settings,
};

#[derive(Clone)]
pub struct AppState {
    inner: Arc<Mutex<Inner>>,
    scanner: Arc<Mutex<LocalScanner>>,
    settings_path: Arc<PathBuf>,
    local_refreshing: Arc<AtomicBool>,
    remote_refreshing: Arc<AtomicBool>,
}

struct Inner {
    settings: Settings,
    local_sessions: Vec<Session>,
    remote_sessions: Vec<Session>,
    discovered_remote_hosts: Vec<String>,
    remote_errors: BTreeMap<String, String>,
    remote_config_error: Option<String>,
    local_error: Option<String>,
    remote_loading: bool,
    remote_generation: u64,
}

impl AppState {
    pub fn new(codex_home: PathBuf, settings_path: PathBuf) -> Self {
        let loaded = settings::load(&settings_path);
        Self {
            inner: Arc::new(Mutex::new(Inner {
                settings: loaded,
                local_sessions: Vec::new(),
                remote_sessions: Vec::new(),
                discovered_remote_hosts: remote::discover_ssh_hosts(),
                remote_errors: BTreeMap::new(),
                remote_config_error: None,
                local_error: None,
                remote_loading: false,
                remote_generation: 0,
            })),
            scanner: Arc::new(Mutex::new(LocalScanner::new(codex_home))),
            settings_path: Arc::new(settings_path),
            local_refreshing: Arc::new(AtomicBool::new(false)),
            remote_refreshing: Arc::new(AtomicBool::new(false)),
        }
    }

    pub fn start_background_refresh(&self, app: AppHandle) {
        let local_state = self.clone();
        let local_app = app.clone();
        thread::spawn(move || {
            loop {
                local_state.refresh_local(&local_app);
                thread::sleep(Duration::from_secs(2));
            }
        });

        let remote_state = self.clone();
        thread::spawn(move || {
            loop {
                remote_state.refresh_remote(&app);
                thread::sleep(Duration::from_secs(15));
            }
        });
    }

    pub fn refresh_all(&self, app: &AppHandle) {
        let local = self.clone();
        let local_app = app.clone();
        thread::spawn(move || local.refresh_local(&local_app));
        self.trigger_remote_refresh(app);
    }

    pub fn trigger_remote_refresh(&self, app: &AppHandle) {
        let state = self.clone();
        let app = app.clone();
        thread::spawn(move || state.refresh_remote(&app));
    }

    fn refresh_local(&self, app: &AppHandle) {
        if self.local_refreshing.swap(true, Ordering::AcqRel) {
            return;
        }
        let result = self
            .scanner
            .lock()
            .map_err(|_| "本地扫描器状态不可用".to_string())
            .and_then(|mut scanner| scanner.scan_sessions());
        if let Ok(mut inner) = self.inner.lock() {
            match result {
                Ok(sessions) => {
                    inner.local_sessions = sessions;
                    inner.local_error = None;
                }
                Err(error) => inner.local_error = Some(error),
            }
        }
        self.local_refreshing.store(false, Ordering::Release);
        self.publish(app);
    }

    fn refresh_remote(&self, app: &AppHandle) {
        if self.remote_refreshing.swap(true, Ordering::AcqRel) {
            return;
        }
        let (hosts, generation) = {
            let mut inner = self
                .inner
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            inner.remote_loading = !inner.settings.remote_hosts.is_empty();
            (inner.settings.remote_hosts.clone(), inner.remote_generation)
        };
        self.publish(app);

        if hosts.is_empty() {
            if let Ok(mut inner) = self.inner.lock() {
                inner.remote_sessions.clear();
                inner.remote_errors.clear();
                inner.remote_loading = false;
            }
            self.remote_refreshing.store(false, Ordering::Release);
            self.publish(app);
            return;
        }

        let handles = hosts
            .into_iter()
            .map(|host| {
                thread::spawn(move || match remote::scan_host(&host) {
                    Ok(sessions) => (host, sessions, None),
                    Err(error) => (host, Vec::new(), Some(error)),
                })
            })
            .collect::<Vec<_>>();
        let results = handles
            .into_iter()
            .filter_map(|handle| handle.join().ok())
            .collect::<Vec<_>>();

        let mut stale = false;
        if let Ok(mut inner) = self.inner.lock() {
            if inner.remote_generation == generation {
                inner.remote_sessions = results
                    .iter()
                    .flat_map(|(_, sessions, _)| sessions.clone())
                    .collect();
                inner.remote_errors = results
                    .into_iter()
                    .filter_map(|(host, _, error)| error.map(|error| (host, error)))
                    .collect();
            } else {
                stale = true;
            }
            inner.remote_loading = false;
        }
        self.remote_refreshing.store(false, Ordering::Release);
        self.publish(app);
        if stale {
            self.trigger_remote_refresh(app);
        }
    }

    pub fn publish(&self, app: &AppHandle) {
        let published = self.published_state();
        update_tray(app, &published);
        let _ = app.emit("codex-pulse://state", published);
    }

    pub fn published_state(&self) -> PublishedState {
        let inner = self
            .inner
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let local = tracked_sessions(&inner.local_sessions, &inner.settings);
        let remote = tracked_sessions(&inner.remote_sessions, &inner.settings);
        PublishedState {
            sessions: local,
            remote_sessions: remote,
            remote_hosts: inner.settings.remote_hosts.clone(),
            discovered_remote_hosts: inner.discovered_remote_hosts.clone(),
            remote_errors: inner.remote_errors.clone(),
            remote_config_error: inner.remote_config_error.clone(),
            error: inner.local_error.clone(),
            remote_loading: inner.remote_loading,
            yolo_enabled: inner.settings.yolo_enabled,
            window_pinned: inner.settings.window_pinned,
            session_title_mode: inner.settings.session_title_mode.clone(),
            display_limits: inner.settings.display_limits.clone(),
            title_lines: inner.settings.title_lines,
        }
    }

    pub fn settings(&self) -> Settings {
        self.inner
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .settings
            .clone()
    }

    pub fn session(&self, id: &str) -> Option<Session> {
        let inner = self.inner.lock().ok()?;
        inner
            .local_sessions
            .iter()
            .chain(inner.remote_sessions.iter())
            .find(|session| session.id == id)
            .cloned()
    }

    pub fn set_yolo(&self, enabled: bool) -> Result<(), String> {
        self.update_settings(|settings| settings.yolo_enabled = enabled)
    }

    pub fn set_window_pinned(&self, pinned: bool) -> Result<(), String> {
        self.update_settings(|settings| settings.window_pinned = pinned)
    }

    pub fn set_session_title_mode(&self, mode: &str) -> Result<(), String> {
        let mode = if mode == "title" { "title" } else { "prompt" }.to_string();
        self.update_settings(|settings| settings.session_title_mode = mode)
    }

    pub fn set_display_preferences(
        &self,
        limits: DisplayLimits,
        title_lines: u8,
    ) -> Result<(), String> {
        self.update_settings(|settings| {
            settings.display_limits = limits.normalized();
            settings.title_lines = if title_lines == 2 { 2 } else { 1 };
        })
    }

    pub fn acknowledge_completion(&self, id: &str, completion_key: &str) -> Result<(), String> {
        let session = self.session(id).ok_or("未找到要确认的会话")?;
        if session.completion_key.as_deref() != Some(completion_key) {
            return Err("完成状态已经变化，请刷新后重试".into());
        }
        let key = completion_key.to_string();
        self.update_settings(|settings| {
            if !settings.acknowledged_completions.contains(&key) {
                settings.acknowledged_completions.push(key);
                settings.acknowledged_completions.sort();
            }
        })
    }

    pub fn reload_ssh_hosts(&self) {
        if let Ok(mut inner) = self.inner.lock() {
            inner.discovered_remote_hosts = remote::discover_ssh_hosts();
            inner.remote_config_error = None;
        }
    }

    pub fn add_remote_host(&self, host: &str) -> Result<(), String> {
        let host = host.trim();
        if !remote::is_valid_host(host) {
            if let Ok(mut inner) = self.inner.lock() {
                inner.remote_config_error =
                    Some("请输入有效的 SSH 主机或 ~/.ssh/config 别名".into());
            }
            return Err("请输入有效的 SSH 主机或 ~/.ssh/config 别名".into());
        }
        let host = host.to_string();
        let mut inner = self.inner.lock().map_err(|_| "应用状态不可用")?;
        inner.remote_config_error = None;
        if !inner.settings.remote_hosts.contains(&host) {
            inner.settings.remote_hosts.push(host);
            inner.settings.remote_hosts.sort();
            inner.remote_generation += 1;
            settings::save(&self.settings_path, &inner.settings)
                .map_err(|error| error.to_string())?;
        }
        Ok(())
    }

    pub fn remove_remote_host(&self, host: &str) -> Result<(), String> {
        let mut inner = self.inner.lock().map_err(|_| "应用状态不可用")?;
        if !inner
            .settings
            .remote_hosts
            .iter()
            .any(|value| value == host)
        {
            return Ok(());
        }
        inner.settings.remote_hosts.retain(|value| value != host);
        inner
            .remote_sessions
            .retain(|session| session.remote_host.as_deref() != Some(host));
        inner.remote_errors.remove(host);
        inner.remote_config_error = None;
        inner.remote_generation += 1;
        settings::save(&self.settings_path, &inner.settings).map_err(|error| error.to_string())
    }

    pub fn rename_session(&self, id: &str, name: &str) -> Result<(), String> {
        let session = self.session(id).ok_or("未找到要重命名的会话")?;
        let name = clean_string(name, 100);
        if name.is_empty() {
            return Err("会话名称不能为空".into());
        }
        if session.source == "remote" {
            remote::manage_session(
                session.remote_host.as_deref().unwrap_or_default(),
                "rename",
                session.remote_session_id.as_deref().unwrap_or_default(),
                &name,
            )?;
        } else {
            self.scanner
                .lock()
                .map_err(|_| "本地扫描器状态不可用")?
                .rename_session(&session.id, &name)?;
        }
        if let Ok(mut inner) = self.inner.lock() {
            for item in &mut inner.local_sessions {
                if item.id == id {
                    item.title.clone_from(&name);
                }
            }
            for item in &mut inner.remote_sessions {
                if item.id == id {
                    item.title.clone_from(&name);
                }
            }
        }
        Ok(())
    }

    pub fn archive_session(&self, id: &str) -> Result<(), String> {
        let session = self.session(id).ok_or("未找到要删除的会话")?;
        if session.resume_blocked() {
            return Err("运行中或等待处理的会话不可删除".into());
        }
        if session.source == "remote" {
            remote::manage_session(
                session.remote_host.as_deref().unwrap_or_default(),
                "archive",
                session.remote_session_id.as_deref().unwrap_or_default(),
                "",
            )?;
        } else {
            self.scanner
                .lock()
                .map_err(|_| "本地扫描器状态不可用")?
                .archive_session(&session.id)?;
        }
        if let Ok(mut inner) = self.inner.lock() {
            inner.local_sessions.retain(|item| item.id != id);
            inner.remote_sessions.retain(|item| item.id != id);
        }
        Ok(())
    }

    pub fn terminate_remote_session(&self, id: &str) -> Result<Session, String> {
        let session = self.session(id).ok_or("未找到要结束的会话")?;
        if session.source != "remote" || !session.has_active_writer() {
            return Err("远程会话当前没有可结束的占用进程".into());
        }
        remote::manage_session(
            session.remote_host.as_deref().unwrap_or_default(),
            "terminate",
            session.remote_session_id.as_deref().unwrap_or_default(),
            &session.pid.unwrap_or_default().to_string(),
        )?;
        Ok(session)
    }

    fn update_settings(&self, update: impl FnOnce(&mut Settings)) -> Result<(), String> {
        let mut inner = self.inner.lock().map_err(|_| "应用状态不可用")?;
        update(&mut inner.settings);
        let now = settings::now_millis();
        inner.settings = inner.settings.clone().normalize(now);
        settings::save(&self.settings_path, &inner.settings).map_err(|error| error.to_string())
    }
}

fn tracked_sessions(sessions: &[Session], settings: &Settings) -> Vec<Session> {
    let acknowledged = settings
        .acknowledged_completions
        .iter()
        .map(String::as_str)
        .collect::<HashSet<_>>();
    let mut sessions = sessions.to_vec();
    for session in &mut sessions {
        let new_completion = session.state == "completed"
            && session
                .completion_key
                .as_deref()
                .is_some_and(|key| !acknowledged.contains(key))
            && session.updated_at > settings.completion_tracking_started_at;
        if new_completion {
            session.state = "completed_pending".into();
            session.detail = "任务已完成，等待你确认".into();
        }
    }
    sessions.sort_by(|left, right| {
        state_priority(&left.state)
            .cmp(&state_priority(&right.state))
            .then_with(|| right.updated_at.cmp(&left.updated_at))
    });
    sessions
}

fn state_priority(state: &str) -> u8 {
    match state {
        "attention" => 0,
        "active" => 1,
        "completed_pending" => 2,
        "failed" => 3,
        "completed" => 4,
        _ => 99,
    }
}

fn update_tray(app: &AppHandle, state: &PublishedState) {
    let Some(tray) = app.tray_by_id("main") else {
        return;
    };
    let sessions = state.sessions.iter().chain(state.remote_sessions.iter());
    let mut attention = 0;
    let mut active = 0;
    let mut completed = 0;
    let mut failed = state.remote_errors.len() + usize::from(state.error.is_some());
    for session in sessions {
        match session.state.as_str() {
            "attention" => attention += 1,
            "active" => active += 1,
            "completed_pending" => completed += 1,
            "failed" => failed += 1,
            _ => {}
        }
    }
    let mut title = if state.yolo_enabled {
        " YOLO".to_string()
    } else {
        String::new()
    };
    if attention + active > 0 {
        title.push_str(&format!(" ▶{}", attention + active));
    }
    if completed > 0 {
        title.push_str(&format!(" ✓{completed}"));
    }
    if failed > 0 {
        title.push_str(&format!(" ×{failed}"));
    }
    let mut summary = Vec::new();
    if attention > 0 {
        summary.push(format!("{attention} 个等待处理"));
    }
    if active > 0 {
        summary.push(format!("{active} 个正在进行"));
    }
    if completed > 0 {
        summary.push(format!("{completed} 个刚完成"));
    }
    if failed > 0 {
        summary.push(format!("{failed} 个失败"));
    }
    if state.yolo_enabled {
        summary.push("YOLO 已开启".into());
    }
    let tooltip = if summary.is_empty() {
        "Codex Pulse · 当前没有需要关注的会话".into()
    } else {
        format!("Codex Pulse · {}", summary.join(" · "))
    };
    let _ = tray.set_title(Some(title));
    let _ = tray.set_tooltip(Some(tooltip));
}

#[cfg(test)]
mod tests {
    use super::*;

    fn session(state: &str, completion_key: Option<&str>) -> Session {
        Session {
            id: "one".into(),
            short_id: "one".into(),
            title: "title".into(),
            last_prompt: "prompt".into(),
            cwd: "/tmp".into(),
            project_name: "tmp".into(),
            source: "cli".into(),
            rollout_path: String::new(),
            state: state.into(),
            detail: String::new(),
            updated_at: 20,
            model: None,
            pid: None,
            writer_owner: None,
            writer_tty: None,
            completion_key: completion_key.map(str::to_string),
            remote_session_id: None,
            remote_host: None,
        }
    }

    #[test]
    fn marks_only_unacknowledged_new_completions() {
        let mut settings = Settings {
            completion_tracking_started_at: 10,
            ..Settings::default()
        };
        let result = tracked_sessions(&[session("completed", Some("turn"))], &settings);
        assert_eq!(result[0].state, "completed_pending");
        settings.acknowledged_completions.push("turn".into());
        let result = tracked_sessions(&[session("completed", Some("turn"))], &settings);
        assert_eq!(result[0].state, "completed");
    }
}
