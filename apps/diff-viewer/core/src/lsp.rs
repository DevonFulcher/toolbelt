//! A read-only LSP client: starts a language server on a checkout of one
//! revision and answers hover and definition requests for its files.

use std::collections::{HashMap, HashSet};
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::atomic::{AtomicI64, Ordering};
use std::sync::{mpsc, Arc, Mutex};
use std::time::Duration;

use anyhow::{anyhow, bail, Context, Result};
use serde::Serialize;
use serde_json::{json, Value};

use crate::git;

const REQUEST_TIMEOUT: Duration = Duration::from_secs(120);

type Pending = Arc<Mutex<HashMap<i64, mpsc::Sender<Value>>>>;

fn write_message(stdin: &Mutex<ChildStdin>, message: &Value) -> Result<()> {
    let body = serde_json::to_vec(message)?;
    let mut stdin = stdin.lock().unwrap();
    write!(stdin, "Content-Length: {}\r\n\r\n", body.len())?;
    stdin.write_all(&body)?;
    stdin.flush()?;
    Ok(())
}

fn read_message(reader: &mut impl BufRead) -> Result<Option<Value>> {
    let mut length = None;
    loop {
        let mut line = String::new();
        if reader.read_line(&mut line)? == 0 {
            return Ok(None);
        }
        let line = line.trim_end();
        if line.is_empty() {
            break;
        }
        if let Some(value) = line.strip_prefix("Content-Length:") {
            length = Some(value.trim().parse::<usize>()?);
        }
    }
    let mut body = vec![0; length.context("message without Content-Length")?];
    reader.read_exact(&mut body)?;
    Ok(Some(serde_json::from_slice(&body)?))
}

pub struct LspClient {
    child: Child,
    stdin: Arc<Mutex<ChildStdin>>,
    pending: Pending,
    next_id: AtomicI64,
    opened: Mutex<HashSet<String>>,
}

impl LspClient {
    pub fn start(command: &[String], root: &Path) -> Result<Self> {
        let (program, args) = command.split_first().context("empty language server command")?;
        let mut child = Command::new(program)
            .args(args)
            .current_dir(root)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .with_context(|| format!("starting {program}"))?;
        let stdin = Arc::new(Mutex::new(child.stdin.take().unwrap()));
        let stdout = child.stdout.take().unwrap();
        let pending: Pending = Arc::default();
        {
            let (stdin, pending) = (stdin.clone(), pending.clone());
            std::thread::spawn(move || {
                let mut reader = BufReader::new(stdout);
                while let Ok(Some(message)) = read_message(&mut reader) {
                    route_message(&stdin, &pending, message);
                }
                pending.lock().unwrap().clear();
            });
        }
        let client = Self { child, stdin, pending, next_id: AtomicI64::new(1), opened: Mutex::default() };
        let root_uri = url::Url::from_directory_path(root).map_err(|()| anyhow!("bad root {root:?}"))?.to_string();
        client.request(
            "initialize",
            json!({
                "processId": std::process::id(),
                "rootUri": root_uri,
                "workspaceFolders": [{ "uri": root_uri, "name": "root" }],
                "capabilities": {
                    "workspace": { "configuration": true },
                    "textDocument": {
                        "hover": { "contentFormat": ["plaintext", "markdown"] },
                        "definition": { "linkSupport": true },
                    },
                },
            }),
        )?;
        client.notify("initialized", json!({}))?;
        Ok(client)
    }

    fn notify(&self, method: &str, params: Value) -> Result<()> {
        write_message(&self.stdin, &json!({ "jsonrpc": "2.0", "method": method, "params": params }))
    }

    pub fn request(&self, method: &str, params: Value) -> Result<Value> {
        let id = self.next_id.fetch_add(1, Ordering::SeqCst);
        let (sender, receiver) = mpsc::channel();
        self.pending.lock().unwrap().insert(id, sender);
        write_message(&self.stdin, &json!({ "jsonrpc": "2.0", "id": id, "method": method, "params": params }))?;
        let response = receiver
            .recv_timeout(REQUEST_TIMEOUT)
            .map_err(|_| anyhow!("{method} got no response (server exited or timed out)"))?;
        if let Some(error) = response.get("error") {
            bail!("{method} failed: {error}");
        }
        Ok(response.get("result").cloned().unwrap_or(Value::Null))
    }

    /// Opens `path` (relative to the root) once; the server reads its text from `text`.
    fn ensure_open(&self, uri: &str, text: &str) -> Result<()> {
        if self.opened.lock().unwrap().insert(uri.to_string()) {
            self.notify(
                "textDocument/didOpen",
                json!({ "textDocument": { "uri": uri, "languageId": "python", "version": 1, "text": text } }),
            )?;
        }
        Ok(())
    }
}

impl Drop for LspClient {
    fn drop(&mut self) {
        let _ = self.child.kill();
    }
}

/// Delivers responses to their waiting request and answers the server's own
/// requests (servers can stall until they get a reply).
fn route_message(stdin: &Mutex<ChildStdin>, pending: &Pending, message: Value) {
    let id = message.get("id").and_then(Value::as_i64);
    match (id, message.get("method").and_then(Value::as_str)) {
        (Some(id), Some(method)) => {
            let result = match method {
                "workspace/configuration" => {
                    let count = message["params"]["items"].as_array().map_or(0, Vec::len);
                    Value::Array(vec![Value::Null; count])
                }
                _ => Value::Null,
            };
            let _ = write_message(stdin, &json!({ "jsonrpc": "2.0", "id": id, "result": result }));
        }
        (Some(id), None) => {
            if let Some(sender) = pending.lock().unwrap().remove(&id) {
                let _ = sender.send(message);
            }
        }
        _ => {}
    }
}

struct Session {
    /// Canonical path of the `git archive` checkout; removed on drop.
    root: PathBuf,
    _checkout: tempfile::TempDir,
    client: LspClient,
}

impl Session {
    fn start(command: &[String], repo: &Path, rev: &str) -> Result<Self> {
        let checkout = tempfile::tempdir()?;
        let mut archive = Command::new("git")
            .arg("-C")
            .arg(repo)
            .args(["archive", rev])
            .stdout(Stdio::piped())
            .spawn()
            .context("running git archive")?;
        let tar = Command::new("tar")
            .arg("-x")
            .arg("-C")
            .arg(checkout.path())
            .stdin(archive.stdout.take().unwrap())
            .status()
            .context("running tar")?;
        if !tar.success() || !archive.wait()?.success() {
            bail!("could not check out {rev}");
        }
        let root = checkout.path().canonicalize()?;
        let client = LspClient::start(command, &root)?;
        Ok(Self { root, _checkout: checkout, client })
    }

    fn uri(&self, path: &str) -> Result<String> {
        let file = self.root.join(path);
        Ok(url::Url::from_file_path(&file).map_err(|()| anyhow!("bad path {file:?}"))?.to_string())
    }

    fn open(&self, path: &str) -> Result<String> {
        let uri = self.uri(path)?;
        let text = std::fs::read_to_string(self.root.join(path)).with_context(|| format!("reading {path}"))?;
        self.client.ensure_open(&uri, &text)?;
        Ok(uri)
    }
}

#[derive(Debug, Serialize)]
pub struct DefinitionTarget {
    /// Path relative to the revision's root, or `None` when the target is
    /// outside the checkout (standard library, installed packages).
    pub path: Option<String>,
    pub uri: String,
    pub line: u32,
    pub character: u32,
}

pub struct LspSessions {
    command: Vec<String>,
    sessions: Mutex<HashMap<(PathBuf, String), Arc<Session>>>,
}

#[derive(Clone, Copy, Debug)]
pub enum Side {
    Old,
    New,
}

impl Side {
    pub fn parse(side: &str) -> Result<Self> {
        match side {
            "old" => Ok(Self::Old),
            "new" => Ok(Self::New),
            other => bail!("unknown side {other}"),
        }
    }

    /// The commit this side shows: the merge base of `base...head`, or `head`.
    pub fn revision(self, repo: &Path, base: &str, head: &str) -> Result<String> {
        match self {
            Self::Old => git::merge_base(repo, base, head),
            Self::New => git::rev_parse(repo, head),
        }
    }
}

impl LspSessions {
    /// `command` is the Python language server's stdio command line.
    pub fn new(command: Vec<String>) -> Self {
        Self { command, sessions: Mutex::default() }
    }

    pub fn from_env() -> Self {
        let command = std::env::var("DV_PYTHON_LSP")
            .unwrap_or_else(|_| "uvx --from basedpyright basedpyright-langserver --stdio".into());
        Self::new(command.split_whitespace().map(String::from).collect())
    }

    fn session(&self, repo: &Path, rev: &str) -> Result<Arc<Session>> {
        let mut sessions = self.sessions.lock().unwrap();
        let key = (repo.to_path_buf(), rev.to_string());
        if let Some(session) = sessions.get(&key) {
            return Ok(session.clone());
        }
        let session = Arc::new(Session::start(&self.command, repo, rev)?);
        sessions.insert(key, session.clone());
        Ok(session)
    }

    fn position_request(
        &self,
        method: &str,
        repo: &Path,
        rev: &str,
        path: &str,
        line: u32,
        character: u32,
    ) -> Result<Value> {
        let session = self.session(repo, rev)?;
        let uri = session.open(path)?;
        session.client.request(
            method,
            json!({ "textDocument": { "uri": uri }, "position": { "line": line, "character": character } }),
        )
    }

    pub fn hover(&self, repo: &Path, rev: &str, path: &str, line: u32, character: u32) -> Result<Value> {
        self.position_request("textDocument/hover", repo, rev, path, line, character)
    }

    pub fn definition(
        &self,
        repo: &Path,
        rev: &str,
        path: &str,
        line: u32,
        character: u32,
    ) -> Result<Option<DefinitionTarget>> {
        let result = self.position_request("textDocument/definition", repo, rev, path, line, character)?;
        let first = match &result {
            Value::Array(items) => items.first(),
            Value::Null => None,
            single => Some(single),
        };
        let Some(location) = first else { return Ok(None) };
        // A `Location` has `uri` and `range`; a `LocationLink` has `targetUri` and `targetSelectionRange`.
        let uri = location.get("targetUri").or_else(|| location.get("uri")).and_then(Value::as_str);
        let range = location.get("targetSelectionRange").or_else(|| location.get("range"));
        let (Some(uri), Some(range)) = (uri, range) else { return Ok(None) };
        let session = self.session(repo, rev)?;
        let path = url::Url::parse(uri)
            .ok()
            .and_then(|u| u.to_file_path().ok())
            .and_then(|p| p.strip_prefix(&session.root).ok().map(|p| p.to_string_lossy().into_owned()));
        Ok(Some(DefinitionTarget {
            path,
            uri: uri.to_string(),
            line: range["start"]["line"].as_u64().unwrap_or(0) as u32,
            character: range["start"]["character"].as_u64().unwrap_or(0) as u32,
        }))
    }
}
