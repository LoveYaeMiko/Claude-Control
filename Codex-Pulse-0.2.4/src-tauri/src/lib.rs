mod commands;
mod models;
mod remote;
mod scanner;
mod settings;
mod state;

use std::{env, path::PathBuf};

use arboard::Clipboard;
use models::DisplayLimits;
use serde_json::Value;
use state::AppState;
use tauri::{
    AppHandle, LogicalSize, Manager, PhysicalPosition, Position, State, WindowEvent,
    image::Image,
    menu::MenuBuilder,
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
};

const WINDOW_WIDTH: f64 = 370.0;
const WINDOW_MIN_HEIGHT: f64 = 190.0;
const WINDOW_MAX_HEIGHT: f64 = 1600.0;

#[tauri::command]
async fn handle_action(
    payload: Value,
    app: AppHandle,
    state: State<'_, AppState>,
) -> Result<(), String> {
    let action = text_field(&payload, "action").unwrap_or_default();
    match action.as_str() {
        "ready" => state.publish(&app),
        "refresh" => state.refresh_all(&app),
        "reloadSSHHosts" => {
            state.reload_ssh_hosts();
            state.publish(&app);
        }
        "addRemoteHost" => {
            let result = state.add_remote_host(&required_text(&payload, "host")?);
            state.publish(&app);
            result?;
            state.trigger_remote_refresh(&app);
        }
        "removeRemoteHost" => {
            state.remove_remote_host(&required_text(&payload, "host")?)?;
            state.publish(&app);
            state.trigger_remote_refresh(&app);
        }
        "remoteConnect" => {
            let host = required_text(&payload, "host")?;
            if !remote::is_valid_host(&host) {
                return Err("SSH 主机名格式无效".into());
            }
            commands::launch_terminal(&format!("ssh {}", commands::shell_quote(&host)))?;
        }
        "setYolo" => {
            state.set_yolo(bool_field(&payload, "enabled")?)?;
            state.publish(&app);
        }
        "setWindowPinned" => {
            let pinned = bool_field(&payload, "pinned")?;
            state.set_window_pinned(pinned)?;
            apply_window_pinned(&app, pinned)?;
            state.publish(&app);
        }
        "setSessionTitleMode" => {
            state.set_session_title_mode(&required_text(&payload, "mode")?)?;
            state.publish(&app);
        }
        "setDisplayPreferences" => {
            let limits = payload.get("displayLimits").cloned().unwrap_or(Value::Null);
            state.set_display_preferences(
                display_limits_from_payload(&limits),
                number_field(&payload, "titleLines").unwrap_or(1) as u8,
            )?;
            state.publish(&app);
        }
        "setWindowHeight" => {
            set_window_height(&app, number_field(&payload, "height").unwrap_or(323) as f64)?;
        }
        "renameSession" => {
            state.rename_session(
                &required_text(&payload, "id")?,
                &required_text(&payload, "name")?,
            )?;
            state.publish(&app);
        }
        "archiveSession" => {
            state.archive_session(&required_text(&payload, "id")?)?;
            state.publish(&app);
        }
        "terminateRemote" => {
            let session = state.terminate_remote_session(&required_text(&payload, "id")?)?;
            commands::launch_terminal(&commands::remote_resume_command(
                &session,
                state.settings().yolo_enabled,
            ))?;
            state.trigger_remote_refresh(&app);
        }
        "minimize" => minimize_window(&app)?,
        "hide" => {
            if let Some(window) = app.get_webview_window("main") {
                window.hide().map_err(|error| error.to_string())?;
            }
        }
        "quit" => app.exit(0),
        "acknowledgeCompletion" => {
            state.acknowledge_completion(
                &required_text(&payload, "id")?,
                &required_text(&payload, "completionKey")?,
            )?;
            state.publish(&app);
        }
        "resume" | "copy" | "reveal" | "remoteResume" | "remoteCopy" => {
            handle_session_action(&action, &payload, &state)?;
        }
        "showSessionMenu" => {}
        _ => return Err("未知操作".into()),
    }
    Ok(())
}

fn handle_session_action(action: &str, payload: &Value, state: &AppState) -> Result<(), String> {
    let session = state
        .session(&required_text(payload, "id")?)
        .ok_or("未找到指定会话")?;
    if matches!(action, "resume" | "copy" | "remoteResume" | "remoteCopy")
        && session.resume_blocked()
    {
        let owner = session
            .writer_owner
            .as_deref()
            .unwrap_or(if session.source == "remote" {
                "远程终端"
            } else {
                "原终端"
            });
        return Err(if session.has_active_writer() {
            format!(
                "检测到 Codex 进程 {} 仍在{owner}中持有这个会话。请回到原终端继续，或结束原进程后再恢复。",
                session.pid.unwrap_or_default()
            )
        } else {
            "这个会话仍在运行或等待操作，请先回到原终端处理。".into()
        });
    }
    let yolo = state.settings().yolo_enabled;
    match action {
        "resume" => commands::launch_terminal(&commands::resume_command(&session, yolo)),
        "copy" => copy_to_clipboard(&commands::resume_command(&session, yolo)),
        "reveal" => commands::reveal_in_finder(&session.cwd),
        "remoteResume" => {
            commands::launch_terminal(&commands::remote_resume_command(&session, yolo))
        }
        "remoteCopy" => copy_to_clipboard(&commands::remote_resume_command(&session, yolo)),
        _ => Ok(()),
    }
}

fn copy_to_clipboard(value: &str) -> Result<(), String> {
    Clipboard::new()
        .and_then(|mut clipboard| clipboard.set_text(value.to_string()))
        .map_err(|error| format!("无法复制到剪贴板：{error}"))
}

fn text_field(payload: &Value, key: &str) -> Option<String> {
    payload.get(key).and_then(Value::as_str).map(str::to_string)
}

fn required_text(payload: &Value, key: &str) -> Result<String, String> {
    text_field(payload, key)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| format!("缺少参数：{key}"))
}

fn bool_field(payload: &Value, key: &str) -> Result<bool, String> {
    payload
        .get(key)
        .and_then(Value::as_bool)
        .ok_or_else(|| format!("参数格式无效：{key}"))
}

fn number_field(payload: &Value, key: &str) -> Option<i64> {
    payload.get(key).and_then(|value| {
        value
            .as_i64()
            .or_else(|| value.as_f64().map(|number| number as i64))
    })
}

fn display_limits_from_payload(payload: &Value) -> DisplayLimits {
    fn limit(payload: &Value, keys: &[&str], fallback: u8) -> u8 {
        keys.iter()
            .find_map(|key| number_field(payload, key))
            .and_then(|value| u8::try_from(value).ok())
            .unwrap_or(fallback)
    }

    DisplayLimits {
        active: limit(payload, &["active"], 4),
        completed_pending: limit(payload, &["completed_pending", "completedPending"], 3),
        failed: limit(payload, &["failed"], 1),
    }
}

fn apply_window_pinned(app: &AppHandle, pinned: bool) -> Result<(), String> {
    let window = app.get_webview_window("main").ok_or("主窗口不可用")?;
    window
        .set_always_on_top(pinned)
        .map_err(|error| error.to_string())?;
    window
        .set_visible_on_all_workspaces(pinned)
        .map_err(|error| error.to_string())
}

fn set_window_height(app: &AppHandle, requested: f64) -> Result<(), String> {
    let window = app.get_webview_window("main").ok_or("主窗口不可用")?;
    let available = window
        .primary_monitor()
        .map_err(|error| error.to_string())?
        .map(|monitor| monitor.work_area().size.height as f64 / monitor.scale_factor())
        .unwrap_or(WINDOW_MAX_HEIGHT)
        - 16.0;
    let height = requested
        .round()
        .clamp(WINDOW_MIN_HEIGHT, WINDOW_MAX_HEIGHT.min(available));
    window
        .set_size(LogicalSize::new(WINDOW_WIDTH, height))
        .map_err(|error| error.to_string())?;
    position_window(&window)
}

fn position_window(window: &tauri::WebviewWindow) -> Result<(), String> {
    let monitor = window
        .current_monitor()
        .map_err(|error| error.to_string())?
        .or(window
            .primary_monitor()
            .map_err(|error| error.to_string())?)
        .ok_or("无法读取屏幕信息")?;
    let area = monitor.work_area();
    let size = window.outer_size().map_err(|error| error.to_string())?;
    let x = area.position.x + area.size.width.saturating_sub(size.width) as i32 - 18;
    let y = area.position.y + 8;
    window
        .set_position(Position::Physical(PhysicalPosition::new(x, y)))
        .map_err(|error| error.to_string())
}

fn show_window(app: &AppHandle) -> Result<(), String> {
    let window = app.get_webview_window("main").ok_or("主窗口不可用")?;
    if window.is_minimized().unwrap_or(false) {
        window.unminimize().map_err(|error| error.to_string())?;
    }
    #[cfg(target_os = "macos")]
    {
        let _ = app.set_dock_visibility(false);
        let _ = app.set_activation_policy(tauri::ActivationPolicy::Accessory);
    }
    position_window(&window)?;
    window.show().map_err(|error| error.to_string())?;
    window.set_focus().map_err(|error| error.to_string())
}

fn toggle_window(app: &AppHandle) {
    let Some(window) = app.get_webview_window("main") else {
        return;
    };
    if window.is_visible().unwrap_or(false) && !window.is_minimized().unwrap_or(false) {
        let _ = window.hide();
    } else {
        let _ = show_window(app);
    }
}

fn minimize_window(app: &AppHandle) -> Result<(), String> {
    let window = app.get_webview_window("main").ok_or("主窗口不可用")?;
    if window.is_minimized().unwrap_or(false) {
        return Ok(());
    }
    #[cfg(target_os = "macos")]
    {
        app.set_activation_policy(tauri::ActivationPolicy::Regular)
            .map_err(|error| error.to_string())?;
        app.set_dock_visibility(true)
            .map_err(|error| error.to_string())?;
    }
    window.minimize().map_err(|error| error.to_string())
}

fn restore_accessory_policy(window: &tauri::Window) {
    #[cfg(target_os = "macos")]
    if !window.is_minimized().unwrap_or(false) {
        let _ = window.app_handle().set_dock_visibility(false);
        let _ = window
            .app_handle()
            .set_activation_policy(tauri::ActivationPolicy::Accessory);
    }
}

fn setup_tray(app: &mut tauri::App) -> tauri::Result<()> {
    let menu = MenuBuilder::new(app)
        .text("toggle-window", "显示或隐藏悬浮窗")
        .text("toggle-yolo", "切换 YOLO 模式")
        .separator()
        .text("quit", "退出 Codex Pulse")
        .build()?;
    let icon = Image::from_bytes(include_bytes!("../icons/32x32.png"))?;
    TrayIconBuilder::with_id("main")
        .icon(icon)
        .icon_as_template(true)
        .tooltip("Codex Pulse")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_tray_icon_event(|tray, event| {
            if matches!(
                event,
                TrayIconEvent::Click {
                    button: MouseButton::Left,
                    button_state: MouseButtonState::Up,
                    ..
                }
            ) {
                toggle_window(tray.app_handle());
            }
        })
        .on_menu_event(|app, event| match event.id().as_ref() {
            "toggle-window" => toggle_window(app),
            "toggle-yolo" => {
                if let Some(state) = app.try_state::<AppState>() {
                    let enabled = !state.settings().yolo_enabled;
                    let _ = state.set_yolo(enabled);
                    state.publish(app);
                }
            }
            "quit" => app.exit(0),
            _ => {}
        })
        .build(app)?;
    Ok(())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _, _| {
            let _ = show_window(app);
        }))
        .invoke_handler(tauri::generate_handler![handle_action])
        .setup(|app| {
            #[cfg(target_os = "macos")]
            {
                app.handle()
                    .set_activation_policy(tauri::ActivationPolicy::Accessory)?;
                app.handle().set_dock_visibility(false)?;
            }
            let codex_home = env::var_os("CODEX_HOME")
                .map(PathBuf::from)
                .or_else(|| dirs::home_dir().map(|home| home.join(".codex")))
                .unwrap_or_else(|| PathBuf::from(".codex"));
            let state = AppState::new(codex_home, settings::default_settings_path());
            let pinned = state.settings().window_pinned;
            app.manage(state.clone());
            setup_tray(app)?;
            apply_window_pinned(app.handle(), pinned).map_err(std::io::Error::other)?;
            state.start_background_refresh(app.handle().clone());
            show_window(app.handle()).map_err(std::io::Error::other)?;
            Ok(())
        })
        .on_window_event(|window, event| match event {
            WindowEvent::CloseRequested { api, .. } => {
                api.prevent_close();
                let _ = window.hide();
            }
            WindowEvent::Focused(true) => restore_accessory_policy(window),
            _ => {}
        })
        .run(tauri::generate_context!())
        .expect("error while running Codex Pulse");
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    #[test]
    fn accepts_camel_and_snake_case_completed_pending_limits() {
        let camel_case = display_limits_from_payload(&json!({
            "active": 2,
            "completedPending": 1,
            "failed": 4
        }));
        assert_eq!(camel_case.completed_pending, 1);

        let snake_case = display_limits_from_payload(&json!({
            "active": 2,
            "completed_pending": 2,
            "failed": 4
        }));
        assert_eq!(snake_case.completed_pending, 2);
    }
}
