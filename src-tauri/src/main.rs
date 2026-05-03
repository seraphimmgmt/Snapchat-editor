#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{Read, Write};
use std::net::TcpListener;
use tauri::{AppHandle, Emitter};
use tauri_plugin_updater::UpdaterExt;

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_process::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_shell::init())
        .invoke_handler(tauri::generate_handler![
            start_oauth_listener,
            open_url,
            debug_log,
            save_temp_file,
            delete_temp_file,
            read_temp_file,
            read_user_file,
            make_temp_dir,
            write_temp_bytes,
            temp_file_exists,
            drive_upload_native,
            jpeg_to_heic,
            ffmpeg_run,
            ffmpeg_version,
            exiftool_run,
            check_updates_now,
            install_update,
            pick_export_folder,
            save_export_to_path,
            reveal_in_folder,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

// ---------- Google OAuth helpers ----------
//
// Google's `disallowed_useragent` policy refuses OAuth in any embedded webview.
// Workaround: open the auth URL in the system browser, run a one-shot HTTP
// server on 127.0.0.1:<random>, and emit the callback URL back to the JS as
// an `oauth-callback` event. The JS does PKCE + token exchange itself.

#[tauri::command]
fn start_oauth_listener(app: AppHandle) -> Result<u16, String> {
    let listener = TcpListener::bind("127.0.0.1:0").map_err(|e| e.to_string())?;
    let port = listener.local_addr().map_err(|e| e.to_string())?.port();

    std::thread::spawn(move || {
        let Ok((mut stream, _)) = listener.accept() else { return };
        let mut buf = [0u8; 8192];
        let n = stream.read(&mut buf).unwrap_or(0);
        let req = String::from_utf8_lossy(&buf[..n]);
        let path = req
            .lines()
            .next()
            .and_then(|l| l.split_whitespace().nth(1))
            .unwrap_or("/")
            .to_string();

        // We try window.close() best-effort, but browsers block it for tabs the
        // user opened themselves (which is our case — the system browser opened
        // this URL via `open`/xdg-open/start, not via window.open). We don't
        // promise auto-close in the UI; we just say "you can close this tab."
        let body = "<!doctype html><meta charset=utf-8><title>Signed in</title>\
            <style>html,body{margin:0;height:100%}\
            body{background:#0a0a0c;color:#f0ede4;font-family:-apple-system,system-ui,sans-serif;\
            display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:24px}\
            .dot{width:10px;height:10px;border-radius:50%;background:linear-gradient(110deg,#C0C0C0,#FFD700,#C0C0C0);background-size:200% 100%;animation:sh 3s linear infinite;box-shadow:0 0 24px rgba(212,175,55,.5);margin-bottom:18px}\
            @keyframes sh{0%{background-position:0% 50%}100%{background-position:200% 50%}}\
            h1{font-size:18px;letter-spacing:.18em;margin:0 0 8px;font-weight:600;text-transform:uppercase}\
            p{color:#8a8a96;margin:0 0 6px;font-size:13px}\
            .hint{margin-top:18px;font-family:ui-monospace,monospace;font-size:11px;color:#666;letter-spacing:.1em}</style>\
            <div class=dot></div><h1>Signed in</h1><p>Returning to Snap Caption Studio…</p><p class=hint>YOU CAN CLOSE THIS TAB</p>\
            <script>setTimeout(function(){try{window.close()}catch(e){}},250);</script>";
        let resp = format!(
            "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
            body.len(),
            body
        );
        let _ = stream.write_all(resp.as_bytes());
        let _ = stream.flush();
        let _ = app.emit("oauth-callback", path);
    });

    Ok(port)
}

// Debug log sink — append messages from JS to /tmp/snapcap-debug.log so the
// outer process can tail it. Strictly a development aid.
#[tauri::command]
fn debug_log(msg: String) {
    use std::fs::OpenOptions;
    use std::io::Write;
    let line = format!("[{}] {}\n",
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_millis().to_string())
            .unwrap_or_default(),
        msg);
    if let Ok(mut f) = OpenOptions::new().create(true).append(true).open("/tmp/snapcap-debug.log") {
        let _ = f.write_all(line.as_bytes());
    }
    eprintln!("[js] {msg}");
}

// ---------- Native file IO + Drive upload + FFmpeg sidecar ----------
//
// These bypass WKWebView entirely for binary-heavy operations:
//   - WebKit's XHR/fetch upload of large blobs to googleapis.com hangs
//     intermittently; reqwest from Rust is reliable.
//   - FFmpeg.wasm has Worker quirks in WKWebView; the native ffmpeg binary
//     bundled as a Tauri sidecar is faster and bug-free.

fn snapcap_temp_dir() -> std::path::PathBuf {
    std::env::temp_dir().join("snapcap")
}

#[tauri::command]
async fn save_temp_file(name: String, bytes: Vec<u8>) -> Result<String, String> {
    let dir = snapcap_temp_dir();
    tokio::fs::create_dir_all(&dir).await.map_err(|e| e.to_string())?;
    let safe_name: String = name
        .chars()
        .map(|c| if c.is_ascii_alphanumeric() || c == '.' || c == '-' || c == '_' { c } else { '_' })
        .collect();
    let unique = format!("{}_{}", uuid::Uuid::new_v4().simple(), safe_name);
    let path = dir.join(unique);
    tokio::fs::write(&path, &bytes).await.map_err(|e| e.to_string())?;
    eprintln!("[save_temp] wrote {} bytes to {}", bytes.len(), path.display());
    Ok(path.to_string_lossy().to_string())
}

#[tauri::command]
async fn delete_temp_file(path: String) -> Result<(), String> {
    // Sandbox to our temp dir so JS can't delete arbitrary files.
    let p = std::path::PathBuf::from(&path);
    if !p.starts_with(snapcap_temp_dir()) {
        return Err(format!("refusing to delete outside snapcap temp: {}", path));
    }
    if p.is_dir() {
        tokio::fs::remove_dir_all(&p).await.map_err(|e| e.to_string())?;
    } else {
        tokio::fs::remove_file(&p).await.map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[tauri::command]
async fn read_temp_file(path: String) -> Result<Vec<u8>, String> {
    let p = std::path::PathBuf::from(&path);
    if !p.starts_with(snapcap_temp_dir()) {
        return Err(format!("refusing to read outside snapcap temp: {}", path));
    }
    tokio::fs::read(&p).await.map_err(|e| e.to_string())
}

// Read a user-dropped file. No sandbox — the user explicitly dropped it onto
// the app, so any path the OS lets us open is fair game. Used by drag-and-drop.
#[tauri::command]
async fn read_user_file(path: String) -> Result<Vec<u8>, String> {
    tokio::fs::read(&path).await.map_err(|e| format!("{e}: {path}"))
}

#[tauri::command]
async fn make_temp_dir(prefix: String) -> Result<String, String> {
    let safe: String = prefix
        .chars()
        .map(|c| if c.is_ascii_alphanumeric() || c == '-' || c == '_' { c } else { '_' })
        .collect();
    let dir = snapcap_temp_dir().join(format!("{}_{}", safe, uuid::Uuid::new_v4().simple()));
    tokio::fs::create_dir_all(&dir).await.map_err(|e| e.to_string())?;
    Ok(dir.to_string_lossy().to_string())
}

#[tauri::command]
async fn write_temp_bytes(dir: String, name: String, bytes: Vec<u8>) -> Result<String, String> {
    let d = std::path::PathBuf::from(&dir);
    if !d.starts_with(snapcap_temp_dir()) {
        return Err(format!("refusing to write outside snapcap temp: {}", dir));
    }
    let safe: String = name
        .chars()
        .map(|c| if c.is_ascii_alphanumeric() || c == '.' || c == '-' || c == '_' { c } else { '_' })
        .collect();
    let path = d.join(safe);
    tokio::fs::write(&path, &bytes).await.map_err(|e| e.to_string())?;
    Ok(path.to_string_lossy().to_string())
}

#[tauri::command]
async fn temp_file_exists(path: String) -> bool {
    tokio::fs::metadata(&path).await.is_ok()
}

#[derive(serde::Serialize)]
struct DriveUploadResult {
    id: String,
    name: String,
    web_view_link: Option<String>,
}

#[tauri::command]
async fn drive_upload_native(
    access_token: String,
    folder_id: String,
    file_name: String,
    mime_type: String,
    file_path: String,
) -> Result<DriveUploadResult, String> {
    use reqwest::header::{AUTHORIZATION, CONTENT_TYPE};

    let bytes = tokio::fs::read(&file_path).await.map_err(|e| format!("read source: {e}"))?;
    let total = bytes.len();
    eprintln!("[drive_upload] uploading {} bytes ({}) to folder {}", total, file_name, folder_id);

    let metadata = serde_json::json!({
        "name": file_name,
        "parents": [folder_id],
    });
    let boundary = format!("snapcap_{}", uuid::Uuid::new_v4().simple());
    let mut body: Vec<u8> = Vec::with_capacity(total + 1024);
    body.extend_from_slice(format!("--{boundary}\r\n").as_bytes());
    body.extend_from_slice(b"Content-Type: application/json; charset=UTF-8\r\n\r\n");
    body.extend_from_slice(metadata.to_string().as_bytes());
    body.extend_from_slice(b"\r\n");
    body.extend_from_slice(format!("--{boundary}\r\n").as_bytes());
    body.extend_from_slice(format!("Content-Type: {}\r\n\r\n", mime_type).as_bytes());
    body.extend_from_slice(&bytes);
    body.extend_from_slice(format!("\r\n--{boundary}--\r\n").as_bytes());

    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(600))
        .build()
        .map_err(|e| e.to_string())?;
    let resp = client
        .post("https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&fields=id,name,webViewLink")
        .header(AUTHORIZATION, format!("Bearer {access_token}"))
        .header(CONTENT_TYPE, format!("multipart/related; boundary={boundary}"))
        .body(body)
        .send()
        .await
        .map_err(|e| format!("send: {e}"))?;

    let status = resp.status();
    let text = resp.text().await.map_err(|e| format!("read response: {e}"))?;
    eprintln!("[drive_upload] HTTP {} body={}", status, &text[..text.len().min(500)]);
    if !status.is_success() {
        return Err(format!("Drive API {status}: {text}"));
    }
    let v: serde_json::Value = serde_json::from_str(&text).map_err(|e| e.to_string())?;
    Ok(DriveUploadResult {
        id: v["id"].as_str().unwrap_or("").to_string(),
        name: v["name"].as_str().unwrap_or(&file_name).to_string(),
        web_view_link: v["webViewLink"].as_str().map(String::from),
    })
}

// Convert a JPEG (already on disk in our temp area) to HEIC.
// macOS: uses the built-in `sips` tool — no extra bundling needed.
// Windows/Linux: invokes the bundled `heif-enc` sidecar (libheif's encoder).
// Both write a sibling `.heic` file in the same temp dir.
#[tauri::command]
async fn jpeg_to_heic(app: tauri::AppHandle, jpeg_path: String) -> Result<String, String> {
    let p = std::path::PathBuf::from(&jpeg_path);
    if !p.starts_with(snapcap_temp_dir()) {
        return Err(format!("refusing to convert outside snapcap temp: {}", jpeg_path));
    }
    let stem = p
        .file_stem()
        .and_then(|s| s.to_str())
        .ok_or("bad jpeg path")?;
    let heic_path = p.with_file_name(format!("{stem}.heic"));
    let heic_str = heic_path.to_string_lossy().to_string();

    #[cfg(target_os = "macos")]
    {
        let _ = app;
        let output = tokio::process::Command::new("sips")
            .args(["-s", "format", "heic", &jpeg_path, "--out", &heic_str])
            .output()
            .await
            .map_err(|e| format!("sips: {e}"))?;
        if !output.status.success() {
            return Err(format!(
                "sips failed: {}",
                String::from_utf8_lossy(&output.stderr)
            ));
        }
        Ok(heic_str)
    }
    #[cfg(not(target_os = "macos"))]
    {
        // libheif's heif-enc: -q quality, -p chroma=420 default. We pin q=92 to
        // roughly match sips' default visual quality.
        use tauri_plugin_shell::ShellExt;
        let output = app
            .shell()
            .sidecar("heif-enc")
            .map_err(|e| format!("heif-enc sidecar lookup: {e}"))?
            .args(["-q", "92", &jpeg_path, "-o", &heic_str])
            .output()
            .await
            .map_err(|e| format!("heif-enc invoke: {e}"))?;
        if !output.status.success() {
            return Err(format!(
                "heif-enc failed: {}",
                String::from_utf8_lossy(&output.stderr)
            ));
        }
        Ok(heic_str)
    }
}

// ExifTool: bundled Perl distribution invoked via system perl on Mac/Linux,
// or the bundled Windows .exe on Windows. Used for richer iPhone EXIF
// injection (full Apple MakerNote etc) than what piexifjs can do.
fn exiftool_path(app: &tauri::AppHandle) -> Result<std::path::PathBuf, String> {
    use tauri::Manager;
    let resource_dir = app
        .path()
        .resource_dir()
        .map_err(|e| format!("resource_dir: {e}"))?;
    Ok(resource_dir.join("exiftool").join("exiftool"))
}

#[tauri::command]
async fn exiftool_run(
    app: tauri::AppHandle,
    args: Vec<String>,
    file_path: Option<String>,
) -> Result<String, String> {
    let exiftool = exiftool_path(&app)?;
    if !exiftool.exists() {
        return Err(format!("exiftool not found at {}", exiftool.display()));
    }
    // Optional file_path is appended last so callers can pass tag-args first.
    let mut full_args = args;
    if let Some(p) = file_path {
        full_args.push(p);
    }

    #[cfg(not(target_os = "windows"))]
    let mut cmd = {
        // Perl distribution: invoke `/usr/bin/perl exiftool ...`
        let mut c = tokio::process::Command::new("/usr/bin/perl");
        c.arg(&exiftool);
        c.args(&full_args);
        c
    };
    #[cfg(target_os = "windows")]
    let mut cmd = {
        // Windows ships exiftool.exe (standalone, includes Perl). Path may be
        // exiftool/exiftool.exe instead of bare exiftool — fall back as needed.
        let exe_path = exiftool.with_extension("exe");
        let prog = if exe_path.exists() { exe_path } else { exiftool };
        let mut c = tokio::process::Command::new(prog);
        c.args(&full_args);
        c
    };

    let output = cmd.output().await.map_err(|e| format!("exiftool spawn: {e}"))?;
    let stdout = String::from_utf8_lossy(&output.stdout).to_string();
    let stderr = String::from_utf8_lossy(&output.stderr).to_string();
    if !output.status.success() {
        return Err(format!(
            "exiftool exited {:?}: {}",
            output.status.code(),
            stderr
        ));
    }
    Ok(stdout)
}

#[tauri::command]
async fn ffmpeg_version(app: tauri::AppHandle) -> Result<String, String> {
    use tauri_plugin_shell::ShellExt;
    let output = app
        .shell()
        .sidecar("ffmpeg")
        .map_err(|e| format!("sidecar lookup: {e}"))?
        .args(["-version"])
        .output()
        .await
        .map_err(|e| format!("ffmpeg invoke: {e}"))?;
    let stdout = String::from_utf8_lossy(&output.stdout).to_string();
    let stderr = String::from_utf8_lossy(&output.stderr).to_string();
    if !output.status.success() {
        return Err(format!("ffmpeg exited {:?}: {}", output.status.code(), stderr));
    }
    Ok(stdout.lines().next().unwrap_or("").to_string())
}

#[tauri::command]
async fn ffmpeg_run(app: tauri::AppHandle, args: Vec<String>, cwd: Option<String>) -> Result<String, String> {
    use tauri_plugin_shell::ShellExt;
    let mut cmd = app
        .shell()
        .sidecar("ffmpeg")
        .map_err(|e| format!("sidecar lookup: {e}"))?
        .args(args.iter().map(String::as_str));
    if let Some(c) = cwd {
        cmd = cmd.current_dir(c);
    }
    let output = cmd.output().await.map_err(|e| format!("ffmpeg invoke: {e}"))?;
    let stderr = String::from_utf8_lossy(&output.stderr).to_string();
    if !output.status.success() {
        return Err(format!(
            "ffmpeg exited {:?}\nstderr:\n{}",
            output.status.code(),
            stderr
        ));
    }
    Ok(stderr) // ffmpeg writes log to stderr even on success
}

#[tauri::command]
fn open_url(url: String) -> Result<(), String> {
    #[cfg(target_os = "macos")]
    let mut cmd = {
        let mut c = std::process::Command::new("open");
        c.arg(&url);
        c
    };
    #[cfg(target_os = "windows")]
    let mut cmd = {
        let mut c = std::process::Command::new("cmd");
        c.args(["/C", "start", "", &url]);
        c
    };
    #[cfg(target_os = "linux")]
    let mut cmd = {
        let mut c = std::process::Command::new("xdg-open");
        c.arg(&url);
        c
    };
    cmd.spawn().map(|_| ()).map_err(|e| e.to_string())
}

// JS-invokable update check. The auto-spawn on startup was removed so the
// frontend can gate it on the user's `autoCheckUpdates` preference.
#[tauri::command]
async fn check_updates_now(app: tauri::AppHandle) -> Result<(), String> {
    check_for_updates(app).await;
    Ok(())
}

// Update flow:
//   - check_for_updates emits `update-available` to the frontend with the new
//     version + release notes. Frontend renders our themed modal and calls
//     install_update if the user clicks INSTALL.
//   - install_update runs download + replace, emits progress + final state.
//   - Frontend can react to events and show progress / errors in the same
//     modal — no native OS dialog popping in.
async fn check_for_updates(handle: tauri::AppHandle) {
    let updater = match handle.updater() {
        Ok(u) => u,
        Err(e) => {
            eprintln!("[updater] init failed: {e}");
            // `update-check-error` is distinct from `update-error` (which the
            // install modal owns) so the Settings-row spinner can react to
            // check failures without clobbering install state.
            let _ = handle.emit(
                "update-check-error",
                serde_json::json!({ "message": format!("init failed: {e}") }),
            );
            return;
        }
    };

    let update = match updater.check().await {
        Ok(Some(u)) => u,
        Ok(None) => {
            eprintln!("[updater] no updates available");
            let _ = handle.emit("update-not-available", serde_json::Value::Null);
            return;
        }
        Err(e) => {
            eprintln!("[updater] check failed: {e}");
            let _ = handle.emit(
                "update-check-error",
                serde_json::json!({ "message": format!("check failed: {e}") }),
            );
            return;
        }
    };

    let version = update.version.clone();
    let body = update.body.clone().unwrap_or_default();
    eprintln!("[updater] update available: {version}");

    // Stash the pending update so install_update can act on it. We use a
    // tauri::State holder via ManagedUpdate so the second command can find it.
    *PENDING_UPDATE.lock().unwrap() = Some(update);

    let _ = handle.emit(
        "update-available",
        serde_json::json!({ "version": version, "body": body }),
    );
}

// Stash for the pending update between check + install. tauri::State is more
// idiomatic but requires generic-bound App access; this Mutex is simpler since
// we only ever have one update in flight.
use std::sync::Mutex;
static PENDING_UPDATE: Mutex<Option<tauri_plugin_updater::Update>> = Mutex::new(None);

#[tauri::command]
async fn install_update(handle: tauri::AppHandle) -> Result<(), String> {
    let update = {
        let mut g = PENDING_UPDATE.lock().map_err(|e| e.to_string())?;
        g.take()
    };
    let update = match update {
        Some(u) => u,
        None => return Err("no update pending".to_string()),
    };

    // Progress callback — we get total length once, then chunk sizes as they
    // come in. Emit a percentage to the frontend for the modal's progress bar.
    let h = handle.clone();
    let total = std::sync::Arc::new(std::sync::Mutex::new(0u64));
    let total_clone = total.clone();
    let downloaded = std::sync::Arc::new(std::sync::Mutex::new(0u64));
    let downloaded_clone = downloaded.clone();

    if let Err(e) = update
        .download_and_install(
            move |chunk_size, content_length| {
                if let Some(len) = content_length {
                    *total_clone.lock().unwrap() = len;
                }
                let mut d = downloaded_clone.lock().unwrap();
                *d += chunk_size as u64;
                let t = *total.lock().unwrap();
                let pct = if t > 0 { (*d as f64 / t as f64) * 100.0 } else { 0.0 };
                let _ = h.emit(
                    "update-progress",
                    serde_json::json!({ "downloaded": *d, "total": t, "percent": pct }),
                );
            },
            || { /* finished */ },
        )
        .await
    {
        eprintln!("[updater] install failed: {e}");
        let _ = handle.emit("update-error", e.to_string());
        return Err(e.to_string());
    }

    let _ = handle.emit("update-installed", serde_json::Value::Null);
    handle.restart();
}

// ---------- Export-to-folder commands ----------
//
// Lets the user pick a default export folder in Settings, then routes the
// editor + carousel exports straight to that folder instead of triggering the
// browser's standard download. Two commands:
//   - pick_export_folder: opens a native folder picker and returns the path.
//   - save_export_to_path: writes the export blob to <dir>/<name>, appending
//     " (n)" before the extension on collision.

#[tauri::command]
async fn pick_export_folder(app: AppHandle) -> Result<Option<String>, String> {
    use tauri_plugin_dialog::DialogExt;
    let (tx, rx) = tokio::sync::oneshot::channel();
    let mut tx_opt = Some(tx);
    app.dialog().file().pick_folder(move |path| {
        if let Some(tx) = tx_opt.take() {
            let _ = tx.send(path);
        }
    });
    let result = rx.await.map_err(|e| e.to_string())?;
    Ok(result.map(|p| p.to_string()))
}

#[tauri::command]
async fn save_export_to_path(
    name: String,
    bytes: Vec<u8>,
    dir: String,
) -> Result<String, String> {
    let dir_path = std::path::PathBuf::from(&dir);
    if !dir_path.is_dir() {
        return Err(format!("not a directory: {}", dir));
    }
    // Sanitize only path-traversal characters; preserve original extension.
    let safe_name: String = name
        .chars()
        .map(|c| if c == '/' || c == '\\' || c == '\0' { '_' } else { c })
        .collect();
    let mut target = dir_path.join(&safe_name);

    if target.exists() {
        let p = std::path::Path::new(&safe_name);
        let stem = p
            .file_stem()
            .map(|s| s.to_string_lossy().to_string())
            .unwrap_or_else(|| safe_name.clone());
        let ext = p.extension().map(|e| e.to_string_lossy().to_string());
        for n in 1..=999 {
            let candidate = match &ext {
                Some(e) => format!("{} ({}).{}", stem, n, e),
                None => format!("{} ({})", stem, n),
            };
            target = dir_path.join(candidate);
            if !target.exists() {
                break;
            }
            if n == 999 {
                return Err("too many filename collisions".to_string());
            }
        }
    }

    tokio::fs::write(&target, &bytes)
        .await
        .map_err(|e| e.to_string())?;
    eprintln!(
        "[export] wrote {} bytes to {}",
        bytes.len(),
        target.display()
    );
    Ok(target.to_string_lossy().to_string())
}

// Reveal a file in the system file manager — Finder on macOS (`open -R`)
// or Explorer on Windows (`explorer /select`). The file gets selected/
// highlighted, not just the parent dir, so the user sees what just landed.
#[tauri::command]
fn reveal_in_folder(path: String) -> Result<(), String> {
    let p = std::path::PathBuf::from(&path);
    if !p.exists() {
        return Err(format!("path does not exist: {}", path));
    }
    #[cfg(target_os = "macos")]
    {
        std::process::Command::new("open")
            .arg("-R")
            .arg(&p)
            .spawn()
            .map(|_| ())
            .map_err(|e| e.to_string())
    }
    #[cfg(target_os = "windows")]
    {
        // /select, expects a backslash-style path. PathBuf already gives us
        // the OS-native form on Windows.
        let arg = format!("/select,{}", p.display());
        std::process::Command::new("explorer")
            .arg(arg)
            .spawn()
            .map(|_| ())
            .map_err(|e| e.to_string())
    }
    #[cfg(target_os = "linux")]
    {
        // Fall back to opening the parent dir; few Linux file managers
        // support a select-on-launch flag that's universally available.
        let parent = p.parent().unwrap_or(std::path::Path::new("."));
        std::process::Command::new("xdg-open")
            .arg(parent)
            .spawn()
            .map(|_| ())
            .map_err(|e| e.to_string())
    }
}
