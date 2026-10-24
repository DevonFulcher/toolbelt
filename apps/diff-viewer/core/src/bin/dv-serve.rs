fn main() -> anyhow::Result<()> {
    let addr = std::env::args().nth(1).unwrap_or_else(|| "127.0.0.1:5274".into());
    eprintln!("diff-viewer-core listening on http://{addr}");
    diff_viewer_core::server::serve(&addr)
}
