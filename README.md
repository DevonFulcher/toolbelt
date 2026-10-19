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

## When `toolbelt git save` and `sync` push

Both run the same sync, which pushes with upstream tracking (`git push -u`), never forced:

- A branch in a tracked stack: when it has children, when origin does not have it yet (a new branch is
  published), when it is strictly ahead of origin, or when it is a leaf with an open PR and something new.
- A branch not in a tracked stack: published if origin lacks it, except a work repo's default branch.
- In a personal repo (GitHub owner outside `tasks.employer_orgs`, or `IS_PERSONAL=true`), an untracked branch
  origin already has, including `main`, is also fast-forwarded. If it diverged from origin nothing is pushed and
  the command stops with an error.
- Not pushed: a work repo's `main` (or other default branch), and its other untracked branches origin already has.
