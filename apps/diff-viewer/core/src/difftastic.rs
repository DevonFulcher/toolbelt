use std::process::Command;

use anyhow::{bail, Context, Result};

/// Runs `difft --display json` on the two texts. The temp files keep `path`'s
/// file name so difftastic picks the language from its extension.
pub fn run(path: &str, old_text: &str, new_text: &str) -> Result<serde_json::Value> {
    let file_name = std::path::Path::new(path)
        .file_name()
        .context("path without a file name")?;
    let dir = tempfile::tempdir()?;
    let old = dir.path().join("old").join(file_name);
    let new = dir.path().join("new").join(file_name);
    for (file, text) in [(&old, old_text), (&new, new_text)] {
        std::fs::create_dir_all(file.parent().unwrap())?;
        std::fs::write(file, text)?;
    }
    let output = Command::new("difft")
        .args(["--display", "json", "--color", "never"])
        .arg(&old)
        .arg(&new)
        .env("DFT_UNSTABLE", "yes")
        .output()
        .context("running difft")?;
    if !output.status.success() {
        bail!("difft failed: {}", String::from_utf8_lossy(&output.stderr));
    }
    serde_json::from_slice(&output.stdout).context("parsing difft JSON")
}
