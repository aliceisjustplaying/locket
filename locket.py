#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import secrets
import shutil
import sys
import time
from pathlib import Path


EXIT_USAGE = 1
EXIT_BAD_LOCK = 3
EXIT_IO = 4

TOKENFILE_NAME = "token"
POLL_SECONDS = 0.1


def locket_dir_for(path: Path) -> Path:
    return path.parent / f"{path.name}.locket"


def token_file_for(locket_dir: Path) -> Path:
    return locket_dir / TOKENFILE_NAME


def write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def require_valid_lock(path: Path, token: str) -> Path:
    locket_dir = locket_dir_for(path)
    token_path = token_file_for(locket_dir)
    if not token_path.exists():
        print(f"Error: no active lock for {path}")
        raise SystemExit(EXIT_BAD_LOCK)
    try:
        current = read_text(token_path).strip()
    except OSError as exc:
        print(f"Error: could not read lock for {path}: {exc}")
        raise SystemExit(EXIT_IO)
    if not secrets.compare_digest(current, hash_token(token)):
        print(f"Error: invalid lock token for {path}")
        raise SystemExit(EXIT_BAD_LOCK)
    return locket_dir


def cmd_lock(args: argparse.Namespace) -> int:
    path = Path(args.path)
    locket_dir = locket_dir_for(path)
    token = secrets.token_hex(4)
    printed_wait = False

    try:
        while True:
            try:
                locket_dir.mkdir(mode=0o700)
                break
            except FileExistsError:
                if not printed_wait:
                    print(f"Waiting for lock: {path}")
                    printed_wait = True
                time.sleep(POLL_SECONDS)
            except OSError as exc:
                print(f"Error: failed to lock {path}: {exc}")
                return EXIT_IO
    except KeyboardInterrupt:
        print(f"Interrupted while waiting for lock: {path}")
        return 130

    try:
        write_text(token_file_for(locket_dir), hash_token(token) + "\n")
    except OSError as exc:
        shutil.rmtree(locket_dir, ignore_errors=True)
        print(f"Error: failed to initialize lock for {path}: {exc}")
        return EXIT_IO

    print(f"Locked, when done run: locket unlock {path} {token}")
    return 0


def cmd_unlock(args: argparse.Namespace) -> int:
    path = Path(args.path)
    token = args.token
    locket_dir = require_valid_lock(path, token)
    try:
        token_file_for(locket_dir).unlink()
        locket_dir.rmdir()
    except OSError as exc:
        print(f"Error: failed to unlock {path}: {exc}")
        return EXIT_IO
    print(f"Unlocked: {path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="locket",
        description="Block until a file lock is available, then unlock it later with the matching token.",
        epilog=(
            "Examples:\n"
            "  locket lock notes.txt\n"
            "  locket unlock notes.txt <token>\n\n"
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
    lock_parser.set_defaults(func=cmd_lock)

    unlock_parser = subparsers.add_parser(
        "unlock",
        help="release a file lock using its token",
        description="Release the lock for PATH if TOKEN matches the current lock.",
    )
    unlock_parser.add_argument("path", help="file to unlock")
    unlock_parser.add_argument("token", help="token returned by 'locket lock'")
    unlock_parser.set_defaults(func=cmd_unlock)

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
