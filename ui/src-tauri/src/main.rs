// GeoScan Control Console — Tauri shell.
//
// The shell owns no domain logic: it spawns the Python JSONL engine host
// (`python -m geoscan.engine_host`), forwards each stdout line to the webview
// as an `engine-message` event, and exposes `engine_send` so the frontend can
// write request lines to the engine's stdin.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{BufRead, BufReader, Write};
use std::path::PathBuf;
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::Mutex;

use tauri::{AppHandle, Emitter, Manager, State, WindowEvent};

struct EngineState {
    stdin: Mutex<Option<ChildStdin>>,
    child: Mutex<Option<Child>>,
}

fn repo_root() -> PathBuf {
    if let Ok(root) = std::env::var("GEOSCAN_REPO") {
        return PathBuf::from(root);
    }
    // Dev layout: <repo>/ui/src-tauri — the repo root is two levels up.
    let manifest = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    manifest
        .parent()
        .and_then(|p| p.parent())
        .map(|p| p.to_path_buf())
        .unwrap_or(manifest)
}

/// Check-and-spawn while holding the child lock the whole time, so two
/// concurrent starts (app setup vs. frontend bootstrap vs. manual restart)
/// can never spawn two engines. Lock order is always child -> stdin.
fn start_engine_locked(app: &AppHandle, state: &EngineState, kill_existing: bool) -> Result<(), String> {
    let mut child_slot = state.child.lock().unwrap();
    if let Some(child) = child_slot.as_mut() {
        let alive = child.try_wait().map_err(|e| e.to_string())?.is_none();
        if alive && !kill_existing {
            return Ok(()); // already running
        }
        if alive {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
    *child_slot = None;

    let python = std::env::var("GEOSCAN_PYTHON").unwrap_or_else(|_| "python".into());
    let root = repo_root();
    let mut cmd = Command::new(&python);
    cmd.args(["-m", "geoscan.engine_host"])
        .current_dir(&root)
        .env("PYTHONPATH", root.join("src"))
        .env("PYTHONIOENCODING", "utf-8")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    }
    let mut child = cmd
        .spawn()
        .map_err(|e| format!("无法启动 Python 引擎（{python}）: {e}"))?;

    let stdout = child.stdout.take().ok_or("engine stdout unavailable")?;
    let stderr = child.stderr.take().ok_or("engine stderr unavailable")?;
    *state.stdin.lock().unwrap() = child.stdin.take();
    *child_slot = Some(child);

    let app_out = app.clone();
    std::thread::spawn(move || {
        for line in BufReader::new(stdout).lines().map_while(Result::ok) {
            let _ = app_out.emit("engine-message", line);
        }
        let _ = app_out.emit("engine-exit", ());
    });
    let app_err = app.clone();
    std::thread::spawn(move || {
        for line in BufReader::new(stderr).lines().map_while(Result::ok) {
            let _ = app_err.emit("engine-stderr", line);
        }
    });
    Ok(())
}

#[tauri::command]
fn engine_start(app: AppHandle, state: State<'_, EngineState>) -> Result<(), String> {
    start_engine_locked(&app, &state, false)
}

/// A real restart: kill the current engine even if it is alive-but-hung,
/// then spawn a fresh one.
#[tauri::command]
fn engine_restart(app: AppHandle, state: State<'_, EngineState>) -> Result<(), String> {
    start_engine_locked(&app, &state, true)
}

#[tauri::command]
fn engine_send(state: State<'_, EngineState>, line: String) -> Result<(), String> {
    let mut guard = state.stdin.lock().unwrap();
    let stdin = guard.as_mut().ok_or("引擎未启动")?;
    stdin
        .write_all(line.as_bytes())
        .and_then(|_| stdin.write_all(b"\n"))
        .and_then(|_| stdin.flush())
        .map_err(|e| e.to_string())
}

fn kill_engine(state: &EngineState) {
    if let Some(mut child) = state.child.lock().unwrap().take() {
        let _ = child.kill();
        let _ = child.wait();
    }
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .manage(EngineState {
            stdin: Mutex::new(None),
            child: Mutex::new(None),
        })
        .invoke_handler(tauri::generate_handler![engine_start, engine_restart, engine_send])
        .setup(|app| {
            let handle = app.handle().clone();
            let state: State<'_, EngineState> = app.state();
            if let Err(error) = engine_start(handle.clone(), state) {
                let _ = handle.emit("engine-stderr", error);
            }
            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::Destroyed = event {
                kill_engine(&window.app_handle().state::<EngineState>());
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
