## `locket`

Use `locket` to coordinate access to shared resources across agents and shells.

`locket` locks a canonical path name. The target path does not need to exist yet, but its parent directory must exist.

Use these patterns:

- For a single command, prefer `locket with-lock <path> -- <command> ...`
- For manual read/edit flows, use:
  1. `locket lock <path>`
  2. read the original resource
  3. edit the original resource
  4. run the exact printed `locket unlock <path> <token>` command

Rules:

1. Lock before reading if the file or resource is shared.
2. Never edit a shared resource without holding its lock.
3. Use stable path names for conceptual locks. If the resource is not a real file yet, pick one canonical placeholder path and keep using it.
4. Do not invent multiple path aliases for the same resource.
5. Never remove `<path>.locket` manually.
6. Never guess tokens, break locks, or treat locks as stale.
7. If lock ownership or path identity looks ambiguous, stop and ask.

Guidance:

- Prefer `locket with-lock` for scripts and one-shot commands.
- Prefer `locket lock -t <seconds>` when waiting forever would be risky.
- Avoid `--absolute-zero` inside active repos unless the user explicitly wants maximum friction. It can break tools that scan the working tree.
- If you change symlinks or hard links while holding a lock, re-check that the canonical lock path still means what you think it means.

Examples:

```sh
locket with-lock docs/plan.md -- make format

locket lock notes/today.md -m "editing weekly summary"
locket unlock /absolute/path/to/notes/today.md abc12345
```
