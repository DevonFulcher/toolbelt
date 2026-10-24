# Moving to a new laptop (`tt machine`)

Two commands: `tt machine export` on the old laptop, then the dotfiles repo's
`bootstrap.sh` on the new one (which runs `tt machine import`). The manual steps
that remain are in the dotfiles README.

## `tt machine export`

Writes one zip and stores it as the 1Password document "toolbelt machine
archive" (`op document create`; an existing document of that title is replaced).
`--to-file PATH` writes the file instead (mode 600). It fails at once, before
reading anything, when `op` is missing or not signed in.

What it carries is one declarative list, `CARRIED_FILES` in
`src/toolbelt/machine_move/archive_layout.py`:

| Carried | Why |
| --- | --- |
| `dotfiles/secrets/*` (`secret.sh`, `machine.sh`, `toolbelt.sh`, token files) | sourced by `.zshenv` / `.zshrc` / the `tt ui` service; gitignored |
| `.env`, `.envrc`, `.setup.sh` of `dotfiles`, `dotfiles-private`, `toolbelt` | gitignored direnv / worktree hooks |
| the toolbelt DB (`stacks.db`) | branches, stacks, tasks, runs, decisions, Slack state, settings, usage |
| the SSH signing key (`user.signingkey`) and the identity files ssh offers `github.com`, with `.pub`, plus `~/.ssh/config` | the private repos are cloned over SSH; restored with mode 600 |
| `~/.claude/projects/*/memory/**` | Claude's per-project memory |
| `~/.zsh_history` | only with `--include-history` |

Not carried: Claude transcripts, caches, histories, `~/.asdf`, `~/.npm`,
virtualenvs, the Claude app's data, `.claude/` and `.cursor/` project folders
inside the repos. The export lists anything else that git ignores in the three
repos, so a new need shows up (`ignored by git and not carried`).

**The DB** is copied with sqlite's online backup (never a file copy of a WAL
database). In the copy the machine-specific tables are emptied:
`transcript_files`, `pr_lookups`, `aggregate_prs`, `aggregate_sync`,
`agent_tool_calls` (they refill on the new machine); a run still `running` is
marked `abandoned`. Everything else is kept.

**SSH keys.** Transferring the private key means two copies of it exist (the old
laptop and the encrypted 1Password document) and it was briefly in a temporary
file. Revoke the old laptop's key on GitHub once the move is done. Use
`--ssh-key PATH` to add keys, `--no-ssh-keys` to carry none (then generate a new
key on the new laptop with `signing_commits.sh` and add it to GitHub).

**Refuses** (unless `--force`) while an autonomous run is running or waiting for
a decision, a decision is pending, or `secrets/machine.sh` says
`IS_PERSONAL=true` (restoring it would turn a work laptop into a personal one).

**In-flight work.** Git does not carry uncommitted changes, commits no remote
has, or stashes. The export prints a checklist of each one found in every
worktree of the registered repos (and the three dotfiles repos). Commit and push
them first.

## `tt machine import`

`--from-file PATH` or, by default, `op document get`. Idempotent:

- files that already match are left alone; a differing file is first moved to
  `~/.toolbelt/machine-import-backups/<timestamp>/`;
- the old home (and git projects directory, if it was elsewhere) is rewritten to
  this laptop's in `repos.path`, `repos.git_common_dir`, worktree claim targets
  (`claim_spans.target`), `usage_calls.cwd`, in text files flagged
  `rewrite_home` (the secrets and memory files), and in the names of
  `~/.claude/projects/<dir>` — so a different user name works;
- a DB that already tracks repos is kept (`--replace-db` backs it up and
  replaces it);
- tracked repos missing here are cloned from their recorded remote
  (`--no-clone-missing` skips that), and the worktrees that existed are
  recreated at the same relative paths when their branch is on origin. A
  clone or branch that cannot be done is listed under "Still to do".

`TOOLBELT_OP` names the `op` executable (tests use a fake one).

## What only a real new laptop verifies

`op` behaviour (`item list`, `document create/edit/get` flags and the sign-in
prompt), Homebrew/cask installs, launchd loading, SSO-authorized SSH clones, and
macOS permission prompts.
