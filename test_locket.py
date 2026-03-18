from __future__ import annotations

import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import locket_core as core


REPO_ROOT = Path(__file__).resolve().parent
CLI = [sys.executable, str(REPO_ROOT / "locket.py")]


class CoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory(prefix="locket-test-")
        self.addCleanup(self.tmpdir.cleanup)
        self.path = Path(self.tmpdir.name) / "note.txt"
        self.path.write_text("hello\n", encoding="utf-8")

    def test_status_treats_disappearing_lock_as_unlocked(self) -> None:
        handle = core.acquire(self.path)
        locket_dir = core.locket_dir_for(self.path)
        original_read_token = core.read_token
        original_rename = Path.rename

        def race_read_token(token_path: Path) -> str | None:
            if token_path == core.token_file_for(locket_dir):
                retiring_dir = core.private_locket_dir(locket_dir, "retiring-test")
                original_rename(locket_dir, retiring_dir)
                import shutil

                shutil.rmtree(retiring_dir)
            return original_read_token(token_path)

        with mock.patch.object(core, "read_token", side_effect=race_read_token):
            result = core.status(self.path)

        self.assertEqual(result.kind, core.StatusKind.UNLOCKED)
        self.assertIsNone(result.metadata)
        self.assertIsNone(result.detail)
        self.assertEqual(handle.path, self.path)

    def test_release_rejects_wrong_token(self) -> None:
        handle = core.acquire(self.path)
        with self.assertRaises(core.LockError) as ctx:
            core.release(self.path, handle.token + "x")
        self.assertEqual(ctx.exception.exit_code, core.EXIT_BAD_LOCK)

    def test_acquire_locks_down_public_dir_permissions(self) -> None:
        handle = core.acquire(self.path)
        locket_dir = core.locket_dir_for(self.path)
        self.assertEqual(core.current_mode(locket_dir), core.DEFAULT_PUBLIC_LOCK_DIR_MODE)
        core.release(self.path, handle.token)

    def test_status_restores_public_dir_permissions(self) -> None:
        handle = core.acquire(self.path)
        locket_dir = core.locket_dir_for(self.path)
        result = core.status(self.path)
        self.assertEqual(result.kind, core.StatusKind.LOCKED)
        self.assertEqual(core.current_mode(locket_dir), core.DEFAULT_PUBLIC_LOCK_DIR_MODE)
        core.release(self.path, handle.token)

    def test_absolute_zero_mode_is_restored_after_status(self) -> None:
        handle = core.acquire(
            self.path,
            public_lock_dir_mode=core.ABSOLUTE_ZERO_LOCK_DIR_MODE,
        )
        locket_dir = core.locket_dir_for(self.path)
        self.assertEqual(core.current_mode(locket_dir), core.ABSOLUTE_ZERO_LOCK_DIR_MODE)
        result = core.status(self.path)
        self.assertEqual(result.kind, core.StatusKind.LOCKED)
        self.assertEqual(core.current_mode(locket_dir), core.ABSOLUTE_ZERO_LOCK_DIR_MODE)
        core.release(self.path, handle.token)

    def test_acquire_retries_when_lock_disappears_during_handoff(self) -> None:
        current = core.acquire(self.path)
        locket_dir = core.locket_dir_for(self.path)
        original_rename = Path.rename
        state = {"triggered": False}

        def race_rename(src: Path, dst: Path):
            if (
                src.name.startswith(".note.txt.locket.staging.")
                and dst == locket_dir
                and not state["triggered"]
            ):
                state["triggered"] = True
                try:
                    return original_rename(src, dst)
                except OSError:
                    retiring_dir = core.private_locket_dir(locket_dir, "retiring-test")
                    with core.temporarily_open_public_lock(locket_dir):
                        original_rename(locket_dir, retiring_dir)
                    import shutil

                    shutil.rmtree(retiring_dir)
                    raise
            return original_rename(src, dst)

        with mock.patch.object(Path, "rename", autospec=True, side_effect=race_rename):
            next_handle = core.acquire(self.path, timeout=0.5)

        self.assertNotEqual(current.token, next_handle.token)
        core.release(self.path, next_handle.token)

    def test_acquire_cleans_up_if_publish_step_fails(self) -> None:
        locket_dir = core.locket_dir_for(self.path)

        def fail_set_dir_mode(path: Path, mode: int) -> None:
            if path == locket_dir:
                raise OSError("chmod boom")
            core.set_dir_mode(path, mode)

        with self.assertRaises(core.LockError) as ctx:
            with mock.patch.object(core, "set_dir_mode", side_effect=fail_set_dir_mode):
                core.acquire(self.path)

        self.assertEqual(ctx.exception.exit_code, core.EXIT_IO)
        self.assertFalse(locket_dir.exists(), "failed acquire should not leave a live lock behind")
        self.assertEqual(core.status(self.path).kind, core.StatusKind.UNLOCKED)

    def test_acquire_rejects_locket_suffix(self) -> None:
        target = Path(self.tmpdir.name) / "note.txt.locket"
        with self.assertRaises(core.LockError) as ctx:
            core.acquire(target)
        self.assertIn(".locket", str(ctx.exception))

    def test_acquire_rejects_missing_parent_directory(self) -> None:
        target = Path(self.tmpdir.name) / "nonexistent" / "note.txt"
        with self.assertRaises(core.LockError) as ctx:
            core.acquire(target)
        self.assertIn("parent directory does not exist", str(ctx.exception))

    def test_acquire_reports_parent_path_that_is_not_a_directory(self) -> None:
        parent = Path(self.tmpdir.name) / "not-a-directory"
        parent.write_text("hello\n", encoding="utf-8")
        target = parent / "note.txt"
        with self.assertRaises(core.LockError) as ctx:
            core.acquire(target)
        self.assertIn("not a directory", str(ctx.exception).lower())

    def test_acquire_rejects_locket_suffix_case_insensitive(self) -> None:
        for suffix in (".LOCKET", ".Locket", ".LoCkEt"):
            target = Path(self.tmpdir.name) / f"note.txt{suffix}"
            with self.assertRaises(core.LockError, msg=f"should reject {suffix}"):
                core.acquire(target)

    def test_acquire_rejects_path_inside_locket_dir(self) -> None:
        locket_dir = Path(self.tmpdir.name) / "note.txt.locket"
        locket_dir.mkdir()
        target = locket_dir / "nested"
        with self.assertRaises(core.LockError) as ctx:
            core.acquire(target)
        self.assertIn(".locket", str(ctx.exception))

    def test_resolve_path_expands_home_directory(self) -> None:
        self.assertEqual(
            core.resolve_path("~/note.txt"),
            (Path.home() / "note.txt").resolve(),
        )

    def test_status_reports_broken_symlink_lock_path_as_corrupt(self) -> None:
        lock_path = self.path.parent / f"{self.path.name}.locket"
        lock_path.symlink_to(self.path.parent / "missing-lock-target")
        result = core.status(self.path)
        self.assertEqual(result.kind, core.StatusKind.CORRUPT)

    def test_status_reports_symlinked_lock_directory_as_corrupt(self) -> None:
        lock_path = self.path.parent / f"{self.path.name}.locket"
        actual_lock_dir = self.path.parent / "actual-lock-dir"
        actual_lock_dir.mkdir()
        (actual_lock_dir / core.TOKENFILE_NAME).write_text("token\n", encoding="utf-8")
        lock_path.symlink_to(actual_lock_dir, target_is_directory=True)
        result = core.status(self.path)
        self.assertEqual(result.kind, core.StatusKind.CORRUPT)

    def test_status_reports_blank_token_file_as_corrupt(self) -> None:
        lock_dir = self.path.parent / f"{self.path.name}.locket"
        lock_dir.mkdir()
        (lock_dir / core.TOKENFILE_NAME).write_text("\n", encoding="utf-8")
        result = core.status(self.path)
        self.assertEqual(result.kind, core.StatusKind.CORRUPT)

    def test_status_reports_token_path_directory_as_corrupt(self) -> None:
        lock_dir = self.path.parent / f"{self.path.name}.locket"
        lock_dir.mkdir()
        (lock_dir / core.TOKENFILE_NAME).mkdir()
        result = core.status(self.path)
        self.assertEqual(result.kind, core.StatusKind.CORRUPT)

    def test_release_rejects_blank_token_file(self) -> None:
        lock_dir = self.path.parent / f"{self.path.name}.locket"
        lock_dir.mkdir()
        (lock_dir / core.TOKENFILE_NAME).write_text("\n", encoding="utf-8")
        with self.assertRaises(core.LockError) as ctx:
            core.release(self.path, "")
        self.assertEqual(ctx.exception.exit_code, core.EXIT_BAD_LOCK)


class CLITests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory(prefix="locket-cli-test-")
        self.addCleanup(self.tmpdir.cleanup)
        self.path = Path(self.tmpdir.name) / "note.txt"
        self.path.write_text("hello\n", encoding="utf-8")

    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*CLI, *args],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_lock_output_is_shell_safe_for_paths_with_spaces(self) -> None:
        target = Path(self.tmpdir.name) / "path with spaces" / "note.txt"
        target.parent.mkdir()
        target.write_text("hello\n", encoding="utf-8")
        result = self.run_cli("lock", str(target))
        self.assertEqual(result.returncode, 0, result.stderr)
        # the printed unlock command should be pasteable as-is
        unlock_cmd = result.stdout.strip().removeprefix("Locked, when done run: ")
        shell_result = subprocess.run(
            unlock_cmd, shell=True, capture_output=True, text=True, cwd=REPO_ROOT
        )
        self.assertEqual(shell_result.returncode, 0, shell_result.stderr)
        status = self.run_cli("status", str(target))
        self.assertIn("Unlocked:", status.stdout)

    def test_with_lock_runs_command_and_releases(self) -> None:
        child = [
            sys.executable,
            "-c",
            "import sys; from pathlib import Path; Path(sys.argv[1]).write_text('updated\\n', encoding='utf-8')",
            str(self.path),
        ]
        result = self.run_cli("with-lock", str(self.path), "--", *child)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(self.path.read_text(encoding="utf-8"), "updated\n")
        status = self.run_cli("status", str(self.path))
        self.assertEqual(status.stdout.strip().split(":")[0], "Unlocked")

    def test_lock_absolute_zero_sets_public_dir_mode(self) -> None:
        locked = self.run_cli("lock", str(self.path), "--absolute-zero")
        self.assertEqual(locked.returncode, 0, locked.stderr)
        token = locked.stdout.strip().split()[-1]
        lock_dir = self.path.parent / f"{self.path.name}.locket"
        self.assertEqual(core.current_mode(lock_dir), core.ABSOLUTE_ZERO_LOCK_DIR_MODE)
        unlocked = self.run_cli("unlock", str(self.path), token)
        self.assertEqual(unlocked.returncode, 0, unlocked.stderr)

    def test_with_lock_missing_path_mentions_separator_shape(self) -> None:
        result = self.run_cli("with-lock", "--", "echo")
        self.assertEqual(result.returncode, 2)
        self.assertIn("requires a path before -- and a command after it", result.stderr)

    def test_lock_handoff_under_contention(self) -> None:
        worker_script = textwrap.dedent(
            f"""
            from __future__ import annotations
            import subprocess
            import sys
            import time
            from pathlib import Path

            cli = {CLI!r}
            path = Path({str(self.path)!r})

            for _ in range(8):
                locked = subprocess.run(
                    [*cli, "lock", str(path), "--timeout", "5"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if locked.returncode != 0:
                    print("lock_fail", locked.returncode, locked.stdout, locked.stderr)
                    raise SystemExit(1)
                token = locked.stdout.strip().split()[-1]
                time.sleep(0.01)
                unlocked = subprocess.run(
                    [*cli, "unlock", str(path), token],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if unlocked.returncode != 0:
                    print("unlock_fail", unlocked.returncode, unlocked.stdout, unlocked.stderr)
                    raise SystemExit(1)
            """
        )

        workers = [
            subprocess.Popen(
                [sys.executable, "-c", worker_script],
                cwd=REPO_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(3)
        ]
        outputs = [worker.communicate(timeout=30) for worker in workers]
        for worker, output in zip(workers, outputs, strict=True):
            combined = "".join(output)
            self.assertEqual(worker.returncode, 0, combined)
            self.assertNotIn("corrupt", combined.lower())

        final_status = self.run_cli("status", str(self.path))
        self.assertEqual(final_status.returncode, 0, final_status.stdout + final_status.stderr)
        self.assertIn("Unlocked:", final_status.stdout)

    def test_status_reports_corrupt_public_lock(self) -> None:
        lock_dir = self.path.parent / f"{self.path.name}.locket"
        lock_dir.mkdir()
        result = self.run_cli("status", str(self.path))
        self.assertEqual(result.returncode, core.EXIT_BAD_LOCK)
        self.assertIn("Corrupt lock directory", result.stdout)

    def test_status_reports_regular_file_at_lock_path_as_corrupt(self) -> None:
        lock_path = self.path.parent / f"{self.path.name}.locket"
        lock_path.write_text("not a directory\n", encoding="utf-8")
        result = self.run_cli("status", str(self.path))
        self.assertEqual(result.returncode, core.EXIT_BAD_LOCK)
        self.assertIn("Corrupt lock directory", result.stdout)
        self.assertEqual(result.stderr, "")

    def test_with_lock_missing_executable_reports_error_without_traceback(self) -> None:
        result = self.run_cli("with-lock", str(self.path), "--", "does-not-exist-cmd")
        self.assertEqual(result.returncode, core.EXIT_IO)
        self.assertEqual(result.stdout, "")
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("does-not-exist-cmd", result.stderr)

        status = self.run_cli("status", str(self.path))
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        self.assertIn("Unlocked:", status.stdout)

    def test_with_lock_non_executable_command_reports_error_without_traceback(self) -> None:
        command = Path(self.tmpdir.name) / "not-executable.sh"
        command.write_text("#!/bin/sh\necho hello\n", encoding="utf-8")

        result = self.run_cli("with-lock", str(self.path), "--", str(command))
        self.assertEqual(result.returncode, core.EXIT_IO)
        self.assertEqual(result.stdout, "")
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn(str(command), result.stderr)

        status = self.run_cli("status", str(self.path))
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        self.assertIn("Unlocked:", status.stdout)

    def test_symlinked_cli_finds_core_module(self) -> None:
        symlink_dir = Path(self.tmpdir.name) / "bin"
        symlink_dir.mkdir()
        symlink_path = symlink_dir / "locket"
        symlink_path.symlink_to(REPO_ROOT / "locket.py")
        result = subprocess.run(
            [sys.executable, str(symlink_path), "--help"],
            cwd=self.tmpdir.name,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage: locket", result.stdout)


if __name__ == "__main__":
    unittest.main()
