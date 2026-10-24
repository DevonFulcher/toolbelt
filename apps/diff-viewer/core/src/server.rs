//! Minimal JSON-over-HTTP server so the web UI can run in a plain browser
//! during development; the Tauri app calls the same functions in-process.

use std::collections::HashMap;
use std::path::Path;
use std::sync::Arc;

use anyhow::{anyhow, Result};
use serde::Serialize;
use tiny_http::{Header, Request, Response, Server};

use crate::lsp::{LspSessions, Side};
use crate::{file_diff, file_text, git, ChangedFile};

fn json_response<T: Serialize>(result: Result<T>) -> Response<std::io::Cursor<Vec<u8>>> {
    let (status, body) = match result {
        Ok(value) => (200, serde_json::to_vec(&value).unwrap()),
        Err(error) => (500, serde_json::to_vec(&serde_json::json!({ "error": format!("{error:#}") })).unwrap()),
    };
    Response::from_data(body)
        .with_status_code(status)
        .with_header(Header::from_bytes("Content-Type", "application/json").unwrap())
}

struct Query(HashMap<String, String>);

impl Query {
    fn get(&self, name: &str) -> Result<&str> {
        self.0.get(name).map(String::as_str).ok_or_else(|| anyhow!("missing {name}"))
    }

    fn number(&self, name: &str) -> Result<u32> {
        Ok(self.get(name)?.parse()?)
    }
}

fn handle(request: Request, lsp: &LspSessions) -> Result<()> {
    let url = url::Url::parse(&format!("http://localhost{}", request.url()))?;
    let query = Query(url.query_pairs().into_owned().collect());
    let response = match url.path() {
        "/api/changed_files" => json_response((|| {
            git::changed_files(Path::new(query.get("repo")?), query.get("base")?, query.get("head")?)
        })()),
        "/api/file_diff" => json_response((|| {
            let file = ChangedFile {
                status: query.get("status")?.into(),
                path: query.get("path")?.into(),
                old_path: query.0.get("old_path").cloned(),
            };
            file_diff(Path::new(query.get("repo")?), query.get("base")?, query.get("head")?, &file)
        })()),
        "/api/file_text" => json_response((|| {
            let side = Side::parse(query.get("side")?)?;
            file_text(Path::new(query.get("repo")?), query.get("base")?, query.get("head")?, side, query.get("path")?)
        })()),
        "/api/lsp_hover" => json_response((|| {
            let repo = Path::new(query.get("repo")?);
            let rev = Side::parse(query.get("side")?)?.revision(repo, query.get("base")?, query.get("head")?)?;
            lsp.hover(repo, &rev, query.get("path")?, query.number("line")?, query.number("character")?)
        })()),
        "/api/lsp_definition" => json_response((|| {
            let repo = Path::new(query.get("repo")?);
            let rev = Side::parse(query.get("side")?)?.revision(repo, query.get("base")?, query.get("head")?)?;
            lsp.definition(repo, &rev, query.get("path")?, query.number("line")?, query.number("character")?)
        })()),
        _ => json_response::<()>(Err(anyhow!("not found"))),
    };
    request.respond(response)?;
    Ok(())
}

pub fn serve(addr: &str) -> Result<()> {
    let server = Server::http(addr).map_err(|e| anyhow!("{e}"))?;
    let lsp = Arc::new(LspSessions::from_env());
    for request in server.incoming_requests() {
        let lsp = lsp.clone();
        std::thread::spawn(move || {
            if let Err(error) = handle(request, &lsp) {
                eprintln!("request failed: {error:#}");
            }
        });
    }
    Ok(())
}
