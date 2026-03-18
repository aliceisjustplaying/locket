# locket

A tiny cooperative file lock for shared notes, docs, and other files that should only be read or edited by one process at a time.

```sh
locket lock ~/notes/today.md -m "editing weekly summary"
# => Locked, when done run: locket unlock /Users/me/notes/today.md 1a2b3c4d

# read and edit the original file directly
$EDITOR ~/notes/today.md

locket unlock /Users/me/notes/today.md 1a2b3c4d
```

`locket` is intentionally small:

- one file
- no daemon
- no dependencies
- cooperative locking with a simple shell workflow

## Install

Requires Python 3.7+.

```sh
chmod +x locket.py
ln -s "$(pwd)/locket.py" /usr/local/bin/locket
```

Or run it directly:

```sh
./locket.py lock path/to/file
```

## Usage

Acquire a lock. If another process already holds it, `locket` waits until it becomes available.

```sh
locket lock path/to/file
```

Optionally stop waiting after a fixed amount of time:

```sh
locket lock path/to/file --timeout 30
```

Optionally tag the lock with a message:

```sh
locket lock path/to/file -m "updating project notes"
```

Example output:

```sh
Locked, when done run: locket unlock /absolute/path/to/file 1a2b3c4d
```

Release the lock with the exact token that was printed:

```sh
locket unlock /absolute/path/to/file 1a2b3c4d
```

If the token is wrong, unlock fails.

Check whether a file is locked:

```sh
locket status path/to/file
# => Locked: /absolute/path/to/file (3m ago, updating project notes)
```

## Path resolution

All paths are resolved to their absolute, canonical form before computing the lock directory. This means `locket lock ./notes.txt`, `locket lock notes.txt`, and `locket lock /full/path/to/notes.txt` all produce the same lock, as long as they refer to the same file. Symlinks are resolved too.

The unlock command printed by `locket lock` always uses the resolved path, so you can copy and paste it directly.

## Expected workflow

1. `locket lock <path>`
2. Read the original file at `<path>`
3. Edit the original file at `<path>`
4. Run the printed `locket unlock <path> <token>` command when done

The lock comes before the read. The intended lifecycle is:

```text
lock
read
edit
unlock
```

Not:

```text
read
lock
edit
unlock
```

Do not edit anything inside `<path>.locket/` manually.

## How it works

For a target file like `notes.txt`, `locket` uses a sibling directory:

```text
notes.txt.locket/
  token
  tag
```

Lock acquisition is done by creating and syncing a fully initialized private directory, then atomically renaming it into place. If the public lock directory already exists, `locket` polls until it disappears.

The unlock token is random and only printed to the caller. Unlock succeeds only when the provided token matches the one stored in the lock directory.

The tag file stores a JSON object with a UTC timestamp and an optional message (from `-m`). This is the "tagout" part: anyone who encounters a lock can run `locket status` to see when it was taken and why, without needing the token.

This is cooperative, not enforced. Any process can remove the lock directory directly. The token exists so that only the caller who acquired the lock has the value needed to release it through the normal CLI flow.

## Corrupt locks

The public lock directory is only published after the token and tag files are fully written, so a missing `token` file is treated as corruption rather than a normal crash-recovery case. `locket` detects this state:

- `locket lock` fails instead of reclaiming it automatically.
- `locket status` reports the lock directory as corrupt.

This is the conservative choice. Automatic reclaim can race with a lock holder that is still in the middle of publishing or retiring a lock. If a process crashes after the token is written, the lock stays held. That is by design: the token file means a caller received a token and may still be working.

## Output

Errors, status messages ("Waiting for lock", "Unlocked"), and diagnostics go to stderr. The `locket lock` command prints the unlock command (containing the token) to stdout, so programmatic callers can capture it without parsing around other output.

## Notes

- Locks are advisory. They only help if every editor or script agrees to use `locket`.
- `locket lock` waits until the lock is available unless `--timeout` is set.
- If you interrupt while waiting, no lock is taken.
- If you lose the token, you cannot unlock that lock through the normal CLI flow.
- This is best suited to local workflows and shared conventions, not access control or distributed consensus.

## Help

```sh
locket --help
locket lock --help
locket unlock --help
locket status --help
```
