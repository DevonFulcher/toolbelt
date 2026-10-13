#!/usr/bin/env bash
# Install (or update) the toolbelt-claims Claude Code mod at user scope.
#
# Idempotent: safe to run from a dotfiles bootstrap on every run. The plugin
# folder is this script's own directory, so it follows the checkout wherever it
# lives. Exits 0 with a message when `claude` is not installed. Honours
# CLAUDE_CONFIG_DIR like the CLI does (point it at a scratch dir to try this).
set -euo pipefail

MARKETPLACE="toolbelt"
PLUGIN="toolbelt-claims"
PLUGIN_ID="${PLUGIN}@${MARKETPLACE}"

PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"

if ! command -v claude >/dev/null 2>&1; then
  echo "claude-plugin: 'claude' is not on PATH; skipping ${PLUGIN_ID}. Re-run ${PLUGIN_DIR}/install.sh once Claude Code is installed."
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

if grep -qF "\"id\": \"${PLUGIN_ID}\"" <<<"${plugins}"; then
  claude plugin update "${PLUGIN_ID}" --scope user
else
  claude plugin install "${PLUGIN_ID}" --scope user
fi

echo "claude-plugin: ${PLUGIN_ID} is installed (user scope). Start a new Claude Code session to load it."
