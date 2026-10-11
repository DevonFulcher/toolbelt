#!/usr/bin/env bash
# Run `tt ui` against fake, self-contained data: two demo repos, a few stacks
# and a fake `gh` (no network, no real repo or account is read or written).
#
#   tools/ui-demo/run.sh [--deep N] [--port N] [--dir DIR]
#
# Nothing outside DIR (default: a fresh temp dir) is touched; TOOLBELT_HOME and
# XDG_CONFIG_HOME point inside it.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$here/../.." && pwd)"
deep=0
port=8771
dir=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --deep) deep="$2"; shift 2 ;;
    --port) port="$2"; shift 2 ;;
    --dir) dir="$2"; shift 2 ;;
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
echo "demo data in $dir"
cd "$dir/repos/demo-app"
exec toolbelt ui --port "$port"
