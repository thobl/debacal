# Repository workflow

- Use Git commits for completed changes. The user requested this as the ongoing
  workflow. Make coherent commits, run appropriate checks, and stage only files
  related to the task; preserve unrelated user edits.
- Keep the repository local unless the user explicitly requests a remote or a
  push. No remote is configured for this workspace.
- Do not commit credentials, local planning notes, personal calendar exports,
  SQLite databases, sync mirrors, virtual environments, or editor backups.

## Git commands in this environment

The environment exposes an empty, read-only `.git` directory. This workspace's
Git metadata therefore lives in `.git-local`, with the project root as its work
tree. Use:

```sh
git --git-dir=.git-local status
git --git-dir=.git-local add <paths>
git --git-dir=.git-local commit -m "Describe the change"
```

On a normal checkout where ordinary Git commands work, use those instead. Never
replace or modify the protected `.git` mount from within this environment.
