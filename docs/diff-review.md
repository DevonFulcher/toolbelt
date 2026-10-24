# Diff review

The **Review** view of `tt ui` shows what changed between two revisions of a
tracked repo (`base...head`: from where `head` forked off `base`, to `head`).
Each file is [difftastic](https://difftastic.wilfred.dev)'s structural diff in a
read-only merge view. A branch card's **review changes →** opens it for that
branch against its parent.

Hover shows what a language server says about the code under the pointer;
Cmd/Ctrl-click opens its definition below.

## Language servers

Hover and definitions come from language servers listed in
`~/.config/toolbelt/config.yaml`. Without a `review` section there is one, for
Python; a `language_servers` list replaces it, so list every language wanted.
A file with no server for its extension still gets the structural diff.

```yaml
review:
  language_servers:
    - language: python            # the LSP language id sent with each file
      extensions: [.py]
      command: [uvx, --from, basedpyright, basedpyright-langserver, --stdio]
      settings:                   # what the server is told when it asks for its settings
        python:
          pythonPath: ${repo}/.venv/bin/python   # ${repo}: the tracked repo's checkout
```

`settings` answers the server's `workspace/configuration` requests, by section
name (`python`, or dotted: `python.analysis`). The Python default points the
server at the repo's own `.venv`, so installed packages resolve (the
interpreter of the working tree, which may differ from the commit under
review).

## Why a checkout per revision

A language server reads its project from disk. Each revision (the old side is
the merge base, the new side is `head`) gets its own read-only copy, made with
`git archive` into a temp directory, with one server per language running on
it. This leaves the repo's own checkout, index and worktree list alone, can
show a revision that is not checked out, and is deleted when the server stops
(at most four run at once; the least recently used stops first). Files git does
not track (a `.venv`, `.env`) are not in the copy, which is why settings can
name paths in the repo's checkout through `${repo}`.
