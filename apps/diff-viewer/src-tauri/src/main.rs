use std::path::PathBuf;

use diff_viewer_core::lsp::{DefinitionTarget, LspSessions, Side};
use diff_viewer_core::{file_diff as core_file_diff, file_text as core_file_text, git, ChangedFile, FileDiff};
use serde_json::Value;

fn to_message(error: anyhow::Error) -> String {
    format!("{error:#}")
}

/// Runs blocking work (git, difftastic, language server round trips) off the main thread.
async fn blocking<T: Send + 'static>(work: impl FnOnce() -> anyhow::Result<T> + Send + 'static) -> Result<T, String> {
    tauri::async_runtime::spawn_blocking(work)
        .await
        .map_err(|e| e.to_string())?
        .map_err(to_message)
}

#[tauri::command]
async fn changed_files(repo: PathBuf, base: String, head: String) -> Result<Vec<ChangedFile>, String> {
    blocking(move || git::changed_files(&repo, &base, &head)).await
}

#[tauri::command]
async fn file_diff(repo: PathBuf, base: String, head: String, file: ChangedFile) -> Result<FileDiff, String> {
    blocking(move || core_file_diff(&repo, &base, &head, &file)).await
}

#[tauri::command]
async fn file_text(repo: PathBuf, base: String, head: String, side: String, path: String) -> Result<String, String> {
    blocking(move || core_file_text(&repo, &base, &head, Side::parse(&side)?, &path)).await
}

#[tauri::command]
async fn lsp_hover(
    lsp: tauri::State<'_, std::sync::Arc<LspSessions>>,
    repo: PathBuf,
    base: String,
    head: String,
    side: String,
    path: String,
    line: u32,
    character: u32,
) -> Result<Value, String> {
    let lsp = lsp.inner().clone();
    blocking(move || {
        let rev = Side::parse(&side)?.revision(&repo, &base, &head)?;
        lsp.hover(&repo, &rev, &path, line, character)
    })
    .await
}

#[tauri::command]
async fn lsp_definition(
    lsp: tauri::State<'_, std::sync::Arc<LspSessions>>,
    repo: PathBuf,
    base: String,
    head: String,
    side: String,
    path: String,
    line: u32,
    character: u32,
) -> Result<Option<DefinitionTarget>, String> {
    let lsp = lsp.inner().clone();
    blocking(move || {
        let rev = Side::parse(&side)?.revision(&repo, &base, &head)?;
        lsp.definition(&repo, &rev, &path, line, character)
    })
    .await
}

fn main() {
    tauri::Builder::default()
        .manage(std::sync::Arc::new(LspSessions::from_env()))
        .invoke_handler(tauri::generate_handler![changed_files, file_diff, file_text, lsp_hover, lsp_definition])
        .run(tauri::generate_context!())
        .expect("error while running the diff viewer");
}
