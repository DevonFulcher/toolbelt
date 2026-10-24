use std::path::Path;
use std::process::Command;

use anyhow::{bail, Context, Result};
use serde::Serialize;

#[derive(Debug, Clone, Serialize, serde::Deserialize)]
pub struct ChangedFile {
    /// `A`dded, `M`odified, `D`eleted or `R`enamed.
    pub status: String,
    pub path: String,
    pub old_path: Option<String>,
}

fn git(repo: &Path, args: &[&str]) -> Result<Vec<u8>> {
    let output = Command::new("git")
        .arg("-C")
        .arg(repo)
        .args(args)
        .output()
        .context("running git")?;
    if !output.status.success() {
        bail!("git {} failed: {}", args.join(" "), String::from_utf8_lossy(&output.stderr));
    }
    Ok(output.stdout)
}

pub fn merge_base(repo: &Path, base: &str, head: &str) -> Result<String> {
    let out = git(repo, &["merge-base", base, head])?;
    Ok(String::from_utf8_lossy(&out).trim().to_string())
}

pub fn changed_files(repo: &Path, base: &str, head: &str) -> Result<Vec<ChangedFile>> {
    let range = format!("{base}...{head}");
    let out = git(repo, &["diff", "--name-status", "-M", "-z", &range])?;
    let text = String::from_utf8_lossy(&out);
    let mut fields = text.split('\0').filter(|f| !f.is_empty());
    let mut files = Vec::new();
    while let Some(status) = fields.next() {
        let kind = status.chars().next().unwrap_or('M');
        if kind == 'R' || kind == 'C' {
            let old = fields.next().context("rename without old path")?;
            let new = fields.next().context("rename without new path")?;
            files.push(ChangedFile { status: "R".into(), path: new.into(), old_path: Some(old.into()) });
        } else {
            let path = fields.next().context("status without path")?;
            files.push(ChangedFile { status: kind.to_string(), path: path.into(), old_path: None });
        }
    }
    Ok(files)
}

/// Contents of `path` at `rev`.
pub fn show(repo: &Path, rev: &str, path: &str) -> Result<String> {
    let out = git(repo, &["show", &format!("{rev}:{path}")])?;
    Ok(String::from_utf8_lossy(&out).into_owned())
}

pub fn rev_parse(repo: &Path, rev: &str) -> Result<String> {
    let out = git(repo, &["rev-parse", rev])?;
    Ok(String::from_utf8_lossy(&out).trim().to_string())
}
