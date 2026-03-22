## `locket`

Use `locket` to coordinate access to shared resources across agents and shells.

`locket` locks a canonical path name. The target path does not need to exist yet, but its parent directory must exist.

**When to lock:**

- **Read-only (no edits planned):** Do NOT use locket. Just read the file directly. Multiple agents can safely read the same files concurrently.
- **Read-then-write:** Lock before reading, since you plan to edit based on what you read.
- **Write-only (single command):** Use `locket with-lock`.

Patterns:

- For a single command that **modifies** a resource, prefer `locket with-lock <path> -- <command> ...`
- For flows where you read then edit:
  1. `locket lock <path> --holder-pid $$` (optionally with `-m "reason"` or `-t <seconds>`)
  2. read the original resource (always — it may have changed since your last read)
  3. edit the original resource
  4. run the exact printed `locket unlock <path> <token>` command

Do not use `locket with-lock` to read a shared file. The lock is released before you see the output, so you are acting on unlocked content.

Rules:

1. Never edit a shared resource without holding its lock.
2. Do NOT lock files you only intend to read — this blocks other agents unnecessarily.
3. Lock individual files, not directories — `locket lock path/to/file.ts`, not `locket lock path/to/directory`.
4. Use stable path names for conceptual locks. If the resource is not a real file yet, pick one canonical placeholder path and keep using it.
5. Do not invent multiple path aliases for the same resource.
6. Never remove `<path>.locket` manually.
7. Never guess tokens, break locks, or treat locks as stale. (Stale locks from dead processes are automatically reaped by `locket lock`.)
8. If lock ownership or path identity looks ambiguous, stop and ask.
9. Never issue parallel lock calls on the same path. One lock, one read, one unlock.

Guidance:

- Prefer `locket with-lock` for one-shot commands that modify a resource (formatters, builds).
- Prefer `locket lock -t <seconds>` when waiting forever would be risky.
- Avoid `--absolute-zero` inside active repos unless the user explicitly wants maximum friction. It can break tools that scan the working tree.
- If you change symlinks or hard links while holding a lock, re-check that the canonical lock path still means what you think it means.

Examples:

```sh
locket with-lock docs/plan.md -- make format

locket lock notes/today.md --holder-pid $$ -m "editing weekly summary"
locket unlock /absolute/path/to/notes/today.md abc12345
```
