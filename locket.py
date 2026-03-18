#!/usr/bin/env python3
from __future__ import annotations

import argparse
import errno
import json
import os
import secrets
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


EXIT_USAGE = 1
EXIT_TIMEOUT = 2
EXIT_BAD_LOCK = 3
EXIT_IO = 4

TOKENFILE_NAME = "token"
TAGFILE_NAME = "tag"
POLL_SECONDS = 0.1


def err(msg: str) -> None:
    print(msg, file=sys.stderr)


def resolve_path(raw: str) -> Path:
    return Path(raw).resolve()


def locket_dir_for(path: Path) -> Path:
    return path.parent / f"{path.name}.locket"


def token_file_for(locket_dir: Path) -> Path:
    return locket_dir / TOKENFILE_NAME


def write_text(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def read_token(path: Path) -> str | None:
    try:
        return read_text(path).strip()
    except FileNotFoundError:
        return None


def sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def private_locket_dir(locket_dir: Path, phase: str) -> Path:
    return locket_dir.parent / f".{locket_dir.name}.{phase}.{secrets.token_hex(8)}"


def validate_lock(path: Path, token: str) -> tuple[Path, int] | tuple[Path, None]:
    """Check that a valid lock exists for path with the given token.

    Returns (locket_dir, None) on success, or (locket_dir, exit_code) on failure.
    """
    locket_dir = locket_dir_for(path)
    token_path = token_file_for(locket_dir)
    try:
        current = read_token(token_path)
    except OSError as exc:
        err(f"Error: could not read lock for {path}: {exc}")
        return locket_dir, EXIT_IO
    if current is None:
        if locket_dir.exists():
            err(f"Error: corrupt lock directory for {path}")
        else:
            err(f"Error: no active lock for {path}")
        return locket_dir, EXIT_BAD_LOCK
    if current != token:
        err(f"Error: invalid lock token for {path}")
        return locket_dir, EXIT_BAD_LOCK
    return locket_dir, None


def tag_file_for(locket_dir: Path) -> Path:
    return locket_dir / TAGFILE_NAME


def write_tag(locket_dir: Path, message: str | None) -> None:
    tag = {"locked_at": datetime.now(timezone.utc).isoformat()}
    if message:
        tag["message"] = message
    write_text(tag_file_for(locket_dir), json.dumps(tag, indent=2) + "\n")


def read_tag(locket_dir: Path) -> dict | None:
    tag_path = tag_file_for(locket_dir)
    if not tag_path.exists():
        return None
    try:
        return json.loads(read_text(tag_path))
    except (OSError, json.JSONDecodeError):
        return None


def format_tag(tag: dict) -> str:
    parts = []
    locked_at = tag.get("locked_at")
    if locked_at:
        try:
            dt = datetime.fromisoformat(locked_at)
            delta = datetime.now(timezone.utc) - dt
            seconds = int(delta.total_seconds())
            if seconds < 60:
                parts.append(f"{seconds}s ago")
            elif seconds < 3600:
                parts.append(f"{seconds // 60}m ago")
            else:
                hours = seconds // 3600
                parts.append(f"{hours}h {(seconds % 3600) // 60}m ago")
        except ValueError:
            parts.append(locked_at)
    message = tag.get("message")
    if message:
        parts.append(message)
    return ", ".join(parts)


def cmd_lock(args: argparse.Namespace) -> int:
    path = resolve_path(args.path)
    locket_dir = locket_dir_for(path)
    token = secrets.token_hex(4)
    printed_wait = False
    if args.timeout is not None and args.timeout < 0:
        err("Error: --timeout must be non-negative")
        return EXIT_USAGE
    deadline = None if args.timeout is None else time.monotonic() + args.timeout

    try:
        while True:
            staging_dir = private_locket_dir(locket_dir, "staging")
            try:
                staging_dir.mkdir(mode=0o700)
                sync_dir(staging_dir.parent)
                write_text(token_file_for(staging_dir), token + "\n")
                write_tag(staging_dir, getattr(args, "message", None))
                sync_dir(staging_dir)
                try:
                    staging_dir.rename(locket_dir)
                except OSError as exc:
                    if exc.errno not in (errno.EEXIST, errno.ENOTEMPTY):
                        raise
                    shutil.rmtree(staging_dir, ignore_errors=True)
                    try:
                        existing_token = read_token(token_file_for(locket_dir))
                    except OSError as read_exc:
                        err(f"Error: failed to inspect lock for {path}: {read_exc}")
                        return EXIT_IO
                    if existing_token is None:
                        err(f"Error: corrupt lock directory for {path}")
                        return EXIT_BAD_LOCK
                    if deadline is not None and time.monotonic() >= deadline:
                        err(f"Timed out waiting for lock: {path}")
                        return EXIT_TIMEOUT
                    if not printed_wait:
                        err(f"Waiting for lock: {path}")
                        printed_wait = True
                    sleep_seconds = POLL_SECONDS
                    if deadline is not None:
                        sleep_seconds = min(
                            sleep_seconds,
                            max(0.0, deadline - time.monotonic()),
                        )
                        if sleep_seconds <= 0:
                            err(f"Timed out waiting for lock: {path}")
                            return EXIT_TIMEOUT
                    time.sleep(sleep_seconds)
                    continue
                sync_dir(locket_dir.parent)
                break
            except OSError as exc:
                shutil.rmtree(staging_dir, ignore_errors=True)
                err(f"Error: failed to lock {path}: {exc}")
                return EXIT_IO
    except KeyboardInterrupt:
        err(f"Interrupted while waiting for lock: {path}")
        return 130

    print(f"Locked, when done run: locket unlock {path} {token}")
    return 0


def cmd_unlock(args: argparse.Namespace) -> int:
    path = resolve_path(args.path)
    token = args.token
    locket_dir, error = validate_lock(path, token)
    if error is not None:
        return error
    retiring_dir = private_locket_dir(locket_dir, "retiring")
    try:
        locket_dir.rename(retiring_dir)
        sync_dir(retiring_dir.parent)
        shutil.rmtree(retiring_dir)
        sync_dir(retiring_dir.parent)
    except OSError as exc:
        err(f"Error: failed to unlock {path}: {exc}")
        return EXIT_IO
    err(f"Unlocked: {path}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    path = resolve_path(args.path)
    locket_dir = locket_dir_for(path)
    token_path = token_file_for(locket_dir)
    if not locket_dir.exists():
        print(f"Unlocked: {path}")
        return 0
    try:
        token = read_token(token_path)
    except OSError as exc:
        err(f"Error: could not read lock for {path}: {exc}")
        return EXIT_IO
    if token is None:
        print(f"Corrupt lock directory (missing token file): {path}")
        return EXIT_BAD_LOCK
    tag = read_tag(locket_dir)
    if tag:
        print(f"Locked: {path} ({format_tag(tag)})")
    else:
        print(f"Locked: {path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="locket",
        description="Block until a file lock is available, then unlock it later with the matching token.",
        epilog=(
            "Examples:\n"
            "  locket lock notes.txt\n"
            '  locket lock notes.txt -m "editing weekly summary"\n'
            "  locket lock notes.txt --timeout 30\n"
            "  locket unlock notes.txt <token>\n"
            "  locket status notes.txt\n\n"
            "After 'locket lock', read and edit the original file directly."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    lock_parser = subparsers.add_parser(
        "lock",
        help="wait until a file lock is available, then acquire it",
        description=(
            "Block until PATH can be locked. Prints the unlock command and lock token."
        ),
    )
    lock_parser.add_argument("path", help="file to lock before editing")
    lock_parser.add_argument("-m", "--message", help="tag the lock with a message")
    lock_parser.add_argument(
        "--timeout",
        type=float,
        help="stop waiting after SECONDS instead of blocking forever",
    )
    lock_parser.set_defaults(func=cmd_lock)

    unlock_parser = subparsers.add_parser(
        "unlock",
        help="release a file lock using its token",
        description="Release the lock for PATH if TOKEN matches the current lock.",
    )
    unlock_parser.add_argument("path", help="file to unlock")
    unlock_parser.add_argument("token", help="token returned by 'locket lock'")
    unlock_parser.set_defaults(func=cmd_unlock)

    status_parser = subparsers.add_parser(
        "status",
        help="check whether a file is locked",
        description="Report whether PATH is currently locked, unlocked, or corrupt.",
    )
    status_parser.add_argument("path", help="file to check")
    status_parser.set_defaults(func=cmd_status)

    return parser


def main() -> int:
    parser = build_parser()
    if len(sys.argv) == 1:
        parser.print_help()
        return 0
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
