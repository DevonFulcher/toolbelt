#!/usr/bin/env bash
# Install (or update) the toolbelt Claude Code mods at user scope:
# toolbelt-claims and toolbelt-usage, and register the toolbelt-decisions MCP
# server (`toolbelt decisions mcp`, which `toolbelt` must provide on PATH).
#
# Idempotent: safe to run from a dotfiles bootstrap on every run. The marketplace
# folder is this script's own directory, so it follows the checkout wherever it
# lives. Exits 0 with a message when `claude` is not installed. Honours
# CLAUDE_CONFIG_DIR like the CLI does (point it at a scratch dir to try this).
set -euo pipefail

MARKETPLACE="toolbelt"
PLUGINS=("toolbelt-claims" "toolbelt-usage")

PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"

if ! command -v claude >/dev/null 2>&1; then
  echo "claude-plugin: 'claude' is not on PATH; skipping ${PLUGINS[*]}. Re-run ${PLUGIN_DIR}/install.sh once Claude Code is installed."
  exit 0
fi

# `--json` output is pretty-printed, one "key": "value" per line.
marketplaces="$(claude plugin marketplace list --json)"
plugins="$(claude plugin list --json)"

if grep -qF "\"name\": \"${MARKETPLACE}\"" <<<"${marketplaces}"; then
  if grep -qF "\"path\": \"${PLUGIN_DIR}\"" <<<"${marketplaces}"; then
    claude plugin marketplace update "${MARKETPLACE}"
  else
    # Registered from another checkout: re-point it here.
    echo "claude-plugin: marketplace '${MARKETPLACE}' points elsewhere; re-adding from ${PLUGIN_DIR}"
    claude plugin marketplace remove "${MARKETPLACE}"
    claude plugin marketplace add "${PLUGIN_DIR}"
    plugins="$(claude plugin list --json)"
  fi
else
  claude plugin marketplace add "${PLUGIN_DIR}"
fi

for plugin in "${PLUGINS[@]}"; do
  plugin_id="${plugin}@${MARKETPLACE}"
  if grep -qF "\"id\": \"${plugin_id}\"" <<<"${plugins}"; then
    claude plugin update "${plugin_id}" --scope user
  else
    claude plugin install "${plugin_id}" --scope user
  fi
  echo "claude-plugin: ${plugin_id} is installed (user scope)."
done

# The decisions MCP server is an MCP server, not a mod: registered at user scope
# once, and left alone when it is already there.
MCP_SERVER="toolbelt-decisions"
if claude mcp get "${MCP_SERVER}" >/dev/null 2>&1; then
  echo "claude-plugin: MCP server ${MCP_SERVER} is registered (user scope)."
else
  claude mcp add --scope user "${MCP_SERVER}" -- toolbelt decisions mcp
  echo "claude-plugin: MCP server ${MCP_SERVER} is registered (user scope)."
fi

echo "claude-plugin: start a new Claude Code session to load them."
