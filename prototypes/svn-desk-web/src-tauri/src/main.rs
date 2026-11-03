#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

// Offline UI shell only. No registered commands, shell, FS or network plugins.
// Production SVN IPC must be separately designed, reviewed and tested.
fn main() {
    tauri::Builder::default()
        .run(tauri::generate_context!())
        .expect("Could not run SVN Desk Web Preview");
}
