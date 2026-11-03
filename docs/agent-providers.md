# Agent providers

Interactive-agent usage sources use the `AgentProvider` abstract base class in
`src/toolbelt/agent_providers/base.py`. `ClaudeAgentProvider` reads Claude Code
session transcripts; `CodexAgentProvider` reads the usage events written by the
Codex hooks. Both normalize calls to `UsageCall`, so storage, pricing summaries,
and the UI consume one format.

Claims and activity use the existing shared file contracts instead of a
provider-specific database path. The Claude integration and Codex hooks both
write claim v1 files and activity chunks under `$TOOLBELT_HOME`. Codex usage is
recorded by `PostModelRequest` hooks when token counts are available; the Codex
transcript format is not read.

Codex's `token_usage` object is retained as source data. The provider maps
input, cached-input, and output counts and prices them as OpenAI responses.
Models without a known price remain counted and are listed as unpriced.

The local Codex hook plugin must be installed and its hooks trusted in Codex
for claims, activity, session context, and usage events to be written. See
[`codex-plugin/README.md`](../codex-plugin/README.md).
