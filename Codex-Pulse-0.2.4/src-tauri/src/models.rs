use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Session {
    pub id: String,
    pub short_id: String,
    pub title: String,
    pub last_prompt: String,
    pub cwd: String,
    pub project_name: String,
    pub source: String,
    pub rollout_path: String,
    pub state: String,
    pub detail: String,
    pub updated_at: i64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub model: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pid: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub writer_owner: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub writer_tty: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub completion_key: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub remote_session_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub remote_host: Option<String>,
}

impl Session {
    pub fn has_active_writer(&self) -> bool {
        self.pid.is_some_and(|pid| pid > 0)
    }

    pub fn resume_blocked(&self) -> bool {
        self.has_active_writer() || self.state == "active" || self.state == "attention"
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct DisplayLimits {
    pub active: u8,
    pub completed_pending: u8,
    pub failed: u8,
}

impl Default for DisplayLimits {
    fn default() -> Self {
        Self {
            active: 4,
            completed_pending: 3,
            failed: 1,
        }
    }
}

impl DisplayLimits {
    pub fn normalized(self) -> Self {
        fn count(value: u8, fallback: u8) -> u8 {
            if (1..=8).contains(&value) {
                value
            } else {
                fallback
            }
        }
        let defaults = Self::default();
        Self {
            active: count(self.active, defaults.active),
            completed_pending: count(self.completed_pending, defaults.completed_pending),
            failed: count(self.failed, defaults.failed),
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", default)]
pub struct Settings {
    pub yolo_enabled: bool,
    pub window_pinned: bool,
    pub session_title_mode: String,
    pub display_limits: DisplayLimits,
    pub title_lines: u8,
    pub completion_tracking_started_at: i64,
    pub acknowledged_completions: Vec<String>,
    pub remote_hosts: Vec<String>,
}

impl Default for Settings {
    fn default() -> Self {
        Self {
            yolo_enabled: false,
            window_pinned: false,
            session_title_mode: "prompt".into(),
            display_limits: DisplayLimits::default(),
            title_lines: 1,
            completion_tracking_started_at: 0,
            acknowledged_completions: Vec::new(),
            remote_hosts: Vec::new(),
        }
    }
}

impl Settings {
    pub fn normalize(mut self, now: i64) -> Self {
        self.session_title_mode = if self.session_title_mode == "title" {
            "title".into()
        } else {
            "prompt".into()
        };
        self.display_limits = self.display_limits.normalized();
        self.title_lines = if self.title_lines == 2 { 2 } else { 1 };
        if self.completion_tracking_started_at <= 0 {
            self.completion_tracking_started_at = now;
        }
        self.acknowledged_completions.sort();
        self.acknowledged_completions.dedup();
        self.remote_hosts.sort();
        self.remote_hosts.dedup();
        self
    }
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct PublishedState {
    pub sessions: Vec<Session>,
    pub remote_sessions: Vec<Session>,
    pub remote_hosts: Vec<String>,
    pub discovered_remote_hosts: Vec<String>,
    pub remote_errors: BTreeMap<String, String>,
    pub remote_config_error: Option<String>,
    pub error: Option<String>,
    pub remote_loading: bool,
    pub yolo_enabled: bool,
    pub window_pinned: bool,
    pub session_title_mode: String,
    pub display_limits: DisplayLimits,
    pub title_lines: u8,
}

#[derive(Clone, Debug)]
pub struct DetectedState {
    pub state: String,
    pub detail: String,
    pub updated_at: i64,
    pub last_prompt: Option<String>,
    pub completion_token: Option<String>,
}

#[derive(Clone, Copy, Debug)]
pub struct ProcessInfo {
    pub pid: u32,
    pub has_working_child: bool,
}
