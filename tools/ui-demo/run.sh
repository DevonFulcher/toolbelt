#!/usr/bin/env bash
# Run `tt ui` against fake, self-contained data: two demo repos, a few stacks
# and a fake `gh` (no network, no real repo or account is read or written).
#
#   tools/ui-demo/run.sh [--deep N] [--port N] [--github-port N] [--dir DIR]
#
# Nothing outside DIR (default: a fresh temp dir) is touched; TOOLBELT_HOME and
# XDG_CONFIG_HOME point inside it.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$here/../.." && pwd)"
deep=0
port=8771
github_port=8772
dir=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --deep) deep="$2"; shift 2 ;;
    --port) port="$2"; shift 2 ;;
    --dir) dir="$2"; shift 2 ;;
    --github-port) github_port="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
dir="${dir:-$(mktemp -d "${TMPDIR:-/tmp}/tt-ui-demo.XXXXXX")}"
mkdir -p "$dir/bin"
cp "$here/gh" "$dir/bin/gh"
chmod +x "$dir/bin/gh"

export TOOLBELT_HOME="$dir/home"
export XDG_CONFIG_HOME="$dir/config"
export IS_PERSONAL=true
export PATH="$dir/bin:$PATH"

(cd "$repo_root" && uv run python tools/ui-demo/seed.py "$dir" --deep "$deep")

# The fake GitHub API `toolbelt ui` talks to (it asks `gh auth token` for a
# token, which the fake `gh` answers).
python3 "$here/github_server.py" "$github_port" &
github_pid=$!
trap 'kill "$github_pid" 2>/dev/null || true' EXIT
export TT_GITHUB_API_URL="http://127.0.0.1:$github_port"
echo "demo data in $dir"
cd "$dir/repos/demo-app"
# This checkout's code (not whichever `toolbelt` is first on PATH).
uv run --project "$repo_root" toolbelt ui --port "$port"
