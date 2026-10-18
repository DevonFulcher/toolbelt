# toolbelt

A collection of tools that I use.

## `--task` on `toolbelt git append`

`toolbelt git append` needs `--task` only for repos owned by an employer org:

- The owner is read from the `origin` remote (`git@github.com:owner/repo.git` or
  `https://github.com/owner/repo`).
- Employer orgs are `tasks.employer_orgs` in `~/.config/toolbelt/config.yaml`
  (default `[dbt-labs, fivetran]`, case-insensitive).
- Any other owner is personal: `--task` is optional, and when given it is stored as usual.
- No `origin`, or one that is not a github.com URL, keeps the requirement.
- `IS_PERSONAL=true` waives it everywhere.
- `tt ui` lists a personal repo only when `IS_PERSONAL=true`; elsewhere it shows work repos alone (a repo with no github.com `origin` counts as work).
