//! Diff backend: git for the changed files and their contents, difftastic for
//! the structural diff of each file pair.

pub mod difftastic;
pub mod git;
pub mod lsp;
pub mod server;

use std::path::Path;

use anyhow::Result;
use serde::Serialize;

pub use git::ChangedFile;

#[derive(Debug, Serialize)]
pub struct FileDiff {
    pub path: String,
    pub old_path: Option<String>,
    pub status: String,
    pub old_text: String,
    pub new_text: String,
    /// difftastic's JSON for this file pair, as printed by `--display json`.
    pub difftastic: serde_json::Value,
}

pub fn file_diff(repo: &Path, base: &str, head: &str, file: &ChangedFile) -> Result<FileDiff> {
    let old_path = file.old_path.as_deref().unwrap_or(&file.path);
    let old_text = if file.status == "A" {
        String::new()
    } else {
        git::show(repo, base_for_diff(repo, base, head)?.as_str(), old_path)?
    };
    let new_text = if file.status == "D" {
        String::new()
    } else {
        git::show(repo, head, &file.path)?
    };
    let difftastic = difftastic::run(&file.path, &old_text, &new_text)?;
    Ok(FileDiff {
        path: file.path.clone(),
        old_path: file.old_path.clone(),
        status: file.status.clone(),
        old_text,
        new_text,
        difftastic,
    })
}

/// The old side of `base...head` is the merge base, matching `git diff base...head`.
fn base_for_diff(repo: &Path, base: &str, head: &str) -> Result<String> {
    git::merge_base(repo, base, head)
}

/// Contents of `path` on one side of `base...head`.
pub fn file_text(repo: &Path, base: &str, head: &str, side: lsp::Side, path: &str) -> Result<String> {
    git::show(repo, &side.revision(repo, base, head)?, path)
}
