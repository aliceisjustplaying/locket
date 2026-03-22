from __future__ import annotations

import os
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

    def test_acquire_rejects_broken_symlink_lock_path_as_corrupt(self) -> None:
        lock_path = self.path.parent / f"{self.path.name}.locket"
        lock_path.symlink_to(self.path.parent / "missing-lock-target")
        with self.assertRaises(core.LockError) as ctx:
            core.acquire(self.path, timeout=0)
        self.assertEqual(ctx.exception.exit_code, core.EXIT_BAD_LOCK)

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

    def test_status_reports_invalid_utf8_token_as_corrupt(self) -> None:
        lock_dir = self.path.parent / f"{self.path.name}.locket"
        lock_dir.mkdir()
        (lock_dir / core.TOKENFILE_NAME).write_bytes(b"\xff\n")
        result = core.status(self.path)
        self.assertEqual(result.kind, core.StatusKind.CORRUPT)

    def test_acquire_rejects_unreadable_token_file_as_corrupt(self) -> None:
        lock_dir = self.path.parent / f"{self.path.name}.locket"
        token_path = lock_dir / core.TOKENFILE_NAME
        lock_dir.mkdir()
        token_path.write_text("abcd1234\n", encoding="utf-8")
        token_path.chmod(0)
        with self.assertRaises(core.LockError) as ctx:
            core.acquire(self.path, timeout=0)
        self.assertEqual(ctx.exception.exit_code, core.EXIT_BAD_LOCK)

    def test_status_reports_unreadable_token_file_as_corrupt(self) -> None:
        lock_dir = self.path.parent / f"{self.path.name}.locket"
        token_path = lock_dir / core.TOKENFILE_NAME
        lock_dir.mkdir()
        token_path.write_text("abcd1234\n", encoding="utf-8")
        token_path.chmod(0)
        result = core.status(self.path)
        self.assertEqual(result.kind, core.StatusKind.CORRUPT)

    def test_status_reports_symlinked_token_file_as_corrupt(self) -> None:
        lock_dir = self.path.parent / f"{self.path.name}.locket"
        external_token = self.path.parent / "external-token"
        external_token.write_text("abcd1234\n", encoding="utf-8")
        lock_dir.mkdir()
        (lock_dir / core.TOKENFILE_NAME).symlink_to(external_token)
        result = core.status(self.path)
        self.assertEqual(result.kind, core.StatusKind.CORRUPT)

    def test_status_reports_symlinked_tag_file_as_corrupt(self) -> None:
        lock_dir = self.path.parent / f"{self.path.name}.locket"
        external_tag = self.path.parent / "external-tag"
        external_tag.write_text(
            '{"locked_at": "2026-03-18T12:00:00+00:00", "message": "external"}\n',
            encoding="utf-8",
        )
        lock_dir.mkdir()
        (lock_dir / core.TOKENFILE_NAME).write_text("abcd1234\n", encoding="utf-8")
        (lock_dir / core.TAGFILE_NAME).symlink_to(external_tag)
        result = core.status(self.path)
        self.assertEqual(result.kind, core.StatusKind.CORRUPT)

    def test_status_ignores_invalid_utf8_tag(self) -> None:
        lock_dir = self.path.parent / f"{self.path.name}.locket"
        lock_dir.mkdir()
        (lock_dir / core.TOKENFILE_NAME).write_text("abcd1234\n", encoding="utf-8")
        (lock_dir / core.TAGFILE_NAME).write_bytes(b"\xff\n")
        result = core.status(self.path)
        self.assertEqual(result.kind, core.StatusKind.LOCKED)

    def test_release_rejects_regular_file_lock_path_as_corrupt(self) -> None:
        lock_path = self.path.parent / f"{self.path.name}.locket"
        lock_path.write_text("not a directory\n", encoding="utf-8")
        with self.assertRaises(core.LockError) as ctx:
            core.release(self.path, "abcd1234")
        self.assertEqual(ctx.exception.exit_code, core.EXIT_BAD_LOCK)

    def test_release_rejects_symlinked_lock_directory_without_mutating_it(self) -> None:
        lock_path = self.path.parent / f"{self.path.name}.locket"
        actual_lock_dir = self.path.parent / "actual-lock-dir"
        actual_lock_dir.mkdir()
        (actual_lock_dir / core.TOKENFILE_NAME).write_text("abcd1234\n", encoding="utf-8")
        lock_path.symlink_to(actual_lock_dir, target_is_directory=True)

        with self.assertRaises(core.LockError) as ctx:
            core.release(self.path, "abcd1234")

        self.assertEqual(ctx.exception.exit_code, core.EXIT_BAD_LOCK)
        self.assertTrue(lock_path.is_symlink())
        self.assertTrue(actual_lock_dir.exists())

    def test_release_rejects_symlinked_token_file(self) -> None:
        lock_dir = self.path.parent / f"{self.path.name}.locket"
        external_token = self.path.parent / "external-token"
        external_token.write_text("abcd1234\n", encoding="utf-8")
        lock_dir.mkdir()
        (lock_dir / core.TOKENFILE_NAME).symlink_to(external_token)

        with self.assertRaises(core.LockError) as ctx:
            core.release(self.path, "abcd1234")

        self.assertEqual(ctx.exception.exit_code, core.EXIT_BAD_LOCK)
        self.assertTrue(lock_dir.exists())

    def test_release_rejects_unreadable_token_file(self) -> None:
        lock_dir = self.path.parent / f"{self.path.name}.locket"
        token_path = lock_dir / core.TOKENFILE_NAME
        lock_dir.mkdir()
        token_path.write_text("abcd1234\n", encoding="utf-8")
        token_path.chmod(0)

        with self.assertRaises(core.LockError) as ctx:
            core.release(self.path, "abcd1234")

        self.assertEqual(ctx.exception.exit_code, core.EXIT_BAD_LOCK)

    def test_release_rejects_symlinked_tag_file(self) -> None:
        lock_dir = self.path.parent / f"{self.path.name}.locket"
        external_tag = self.path.parent / "external-tag"
        external_tag.write_text(
            '{"locked_at": "2026-03-18T12:00:00+00:00", "message": "external"}\n',
            encoding="utf-8",
        )
        lock_dir.mkdir()
        (lock_dir / core.TOKENFILE_NAME).write_text("abcd1234\n", encoding="utf-8")
        (lock_dir / core.TAGFILE_NAME).symlink_to(external_tag)

        with self.assertRaises(core.LockError) as ctx:
            core.release(self.path, "abcd1234")

        self.assertEqual(ctx.exception.exit_code, core.EXIT_BAD_LOCK)
        self.assertTrue(lock_dir.exists())

    def test_release_rejects_blank_token_file(self) -> None:
        lock_dir = self.path.parent / f"{self.path.name}.locket"
        lock_dir.mkdir()
        (lock_dir / core.TOKENFILE_NAME).write_text("\n", encoding="utf-8")
        with self.assertRaises(core.LockError) as ctx:
            core.release(self.path, "")
        self.assertEqual(ctx.exception.exit_code, core.EXIT_BAD_LOCK)

    def test_status_wraps_permission_error_opening_public_lock(self) -> None:
        handle = core.acquire(self.path)
        locket_dir = core.locket_dir_for(self.path)
        original_set_dir_mode = core.set_dir_mode

        def fail_set_dir_mode(path: Path, mode: int) -> None:
            if path == locket_dir and mode == core.PRIVATE_LOCK_DIR_MODE:
                raise PermissionError("chmod denied")
            return original_set_dir_mode(path, mode)

        with mock.patch.object(core, "set_dir_mode", side_effect=fail_set_dir_mode):
            with self.assertRaises(core.LockError) as ctx:
                core.status(self.path)

        self.assertEqual(ctx.exception.exit_code, core.EXIT_IO)
        core.release(self.path, handle.token)

    def test_acquire_reaps_lock_from_dead_local_process(self) -> None:
        # Acquire a lock and then fake the metadata to look like it was
        # held by a dead process on the local host.
        handle = core.acquire(self.path)
        locket_dir = core.locket_dir_for(self.path)
        # Use a PID that is guaranteed not to exist (PID 1 is init/launchd
        # and is always alive, so pick a large number unlikely to be in use).
        dead_pid = 2_000_000_000
        fake_metadata = core.LockMetadata(
            locked_at=handle.metadata.locked_at,
            message=handle.metadata.message,
            pid=dead_pid,
            locker_pid=handle.metadata.locker_pid,
            user=handle.metadata.user,
            host=handle.metadata.host,
        )
        # Overwrite the tag file with the dead PID
        with core.temporarily_open_public_lock(locket_dir):
            core.write_metadata(locket_dir, fake_metadata)
        # A new acquire should auto-reap the stale lock and succeed
        new_handle = core.acquire(self.path)
        self.assertNotEqual(new_handle.token, handle.token)
        core.release(self.path, new_handle.token)

    def test_acquire_does_not_reap_lock_from_live_process(self) -> None:
        # Acquire a lock with the current process's PID as holder
        handle = core.acquire(self.path)
        locket_dir = core.locket_dir_for(self.path)
        # Overwrite metadata to use our own PID (which is alive)
        live_metadata = core.LockMetadata(
            locked_at=handle.metadata.locked_at,
            message=handle.metadata.message,
            pid=os.getpid(),
            locker_pid=handle.metadata.locker_pid,
            user=handle.metadata.user,
            host=handle.metadata.host,
        )
        with core.temporarily_open_public_lock(locket_dir):
            core.write_metadata(locket_dir, live_metadata)
        # A new acquire with timeout=0 should fail (lock is not stale)
        with self.assertRaises(core.LockError) as ctx:
            core.acquire(self.path, timeout=0)
        self.assertEqual(ctx.exception.exit_code, core.EXIT_TIMEOUT)
        core.release(self.path, handle.token)

    def test_acquire_does_not_reap_lock_from_remote_host(self) -> None:
        # Acquire a lock, then fake metadata with a dead PID but different host
        handle = core.acquire(self.path)
        locket_dir = core.locket_dir_for(self.path)
        remote_metadata = core.LockMetadata(
            locked_at=handle.metadata.locked_at,
            message=handle.metadata.message,
            pid=2_000_000_000,
            locker_pid=handle.metadata.locker_pid,
            user=handle.metadata.user,
            host="some-other-host.example.com",
        )
        with core.temporarily_open_public_lock(locket_dir):
            core.write_metadata(locket_dir, remote_metadata)
        # Should NOT reap — different host means we can't verify PID liveness
        with self.assertRaises(core.LockError) as ctx:
            core.acquire(self.path, timeout=0)
        self.assertEqual(ctx.exception.exit_code, core.EXIT_TIMEOUT)
        core.release(self.path, handle.token)

    def test_metadata_records_both_pids(self) -> None:
        handle = core.acquire(self.path)
        # pid should be the parent (caller), locker_pid should be us
        self.assertEqual(handle.metadata.pid, os.getppid())
        self.assertEqual(handle.metadata.locker_pid, os.getpid())
        core.release(self.path, handle.token)

    def test_acquire_with_explicit_holder_pid(self) -> None:
        handle = core.acquire(self.path, holder_pid=os.getpid())
        self.assertEqual(handle.metadata.pid, os.getpid())
        self.assertEqual(handle.metadata.locker_pid, os.getpid())
        core.release(self.path, handle.token)

    def test_acquire_rejects_zero_holder_pid(self) -> None:
        with self.assertRaises(core.LockError) as ctx:
            core.acquire(self.path, holder_pid=0)
        self.assertEqual(ctx.exception.exit_code, core.EXIT_USAGE)

    def test_acquire_rejects_negative_holder_pid(self) -> None:
        with self.assertRaises(core.LockError) as ctx:
            core.acquire(self.path, holder_pid=-1)
        self.assertEqual(ctx.exception.exit_code, core.EXIT_USAGE)

    def test_stale_lock_reap_stress(self) -> None:
        """Multiple sequential acquire calls against stale locks should all succeed."""
        dead_pid = 2_000_000_000
        for i in range(10):
            # Plant a stale lock
            handle = core.acquire(self.path)
            locket_dir = core.locket_dir_for(self.path)
            fake_metadata = core.LockMetadata(
                locked_at=handle.metadata.locked_at,
                pid=dead_pid,
                locker_pid=handle.metadata.locker_pid,
                user=handle.metadata.user,
                host=handle.metadata.host,
            )
            with core.temporarily_open_public_lock(locket_dir):
                core.write_metadata(locket_dir, fake_metadata)
            # Acquire should reap and succeed
            new_handle = core.acquire(self.path, timeout=5)
            self.assertNotEqual(new_handle.token, handle.token)
            core.release(self.path, new_handle.token)

    def test_concurrent_stale_reap_and_live_contention(self) -> None:
        """A stale lock followed by live contention should work correctly."""
        dead_pid = 2_000_000_000
        # Plant a stale lock
        handle = core.acquire(self.path)
        locket_dir = core.locket_dir_for(self.path)
        fake_metadata = core.LockMetadata(
            locked_at=handle.metadata.locked_at,
            pid=dead_pid,
            locker_pid=handle.metadata.locker_pid,
            user=handle.metadata.user,
            host=handle.metadata.host,
        )
        with core.temporarily_open_public_lock(locket_dir):
            core.write_metadata(locket_dir, fake_metadata)
        # First acquire should reap the stale lock
        h1 = core.acquire(self.path, holder_pid=os.getpid())
        # Second acquire should block (h1 is alive) and timeout
        with self.assertRaises(core.LockError) as ctx:
            core.acquire(self.path, timeout=0.2)
        self.assertEqual(ctx.exception.exit_code, core.EXIT_TIMEOUT)
        core.release(self.path, h1.token)
        # Now third acquire should succeed
        h2 = core.acquire(self.path, timeout=1)
        core.release(self.path, h2.token)


    def test_cli_stale_reap_with_subprocess_workers(self) -> None:
        """Realistic stress test: subprocess workers lock/edit/unlock a shared file.

        One worker is killed mid-lock to create a stale lock. The remaining
        workers should auto-reap it and continue without manual intervention.
        """
        shared_file = self.path
        shared_file.write_text("", encoding="utf-8")

        worker_script = textwrap.dedent(
            f"""
            import subprocess
            import sys
            import time
            import os
            from pathlib import Path

            cli = {CLI!r}
            path = Path({str(shared_file)!r})
            worker_id = sys.argv[1]
            should_die = sys.argv[2] == "die"

            for i in range(5):
                locked = subprocess.run(
                    [*cli, "lock", str(path), "--holder-pid", str(os.getpid()), "--timeout", "10"],
                    capture_output=True, text=True, check=False,
                )
                if locked.returncode != 0:
                    print(f"W{{worker_id}} iter {{i}}: lock failed: {{locked.stderr}}")
                    raise SystemExit(1)
                token = locked.stdout.strip().split()[-1]

                # Simulate work: append to shared file
                with open(path, "a") as f:
                    f.write(f"worker-{{worker_id}}-iter-{{i}}\\n")

                if should_die and i == 2:
                    # Die mid-lock without unlocking — simulates crashed agent
                    print(f"W{{worker_id}}: dying with lock held (iter {{i}})")
                    raise SystemExit(0)

                unlocked = subprocess.run(
                    [*cli, "unlock", str(path), token],
                    capture_output=True, text=True, check=False,
                )
                if unlocked.returncode != 0:
                    print(f"W{{worker_id}} iter {{i}}: unlock failed: {{unlocked.stderr}}")
                    raise SystemExit(1)

            print(f"W{{worker_id}}: completed all iterations")
            """
        )

        # Start the dying worker first — it will lock, do 3 iterations, then die
        # with the lock held, creating a stale lock.
        dying_worker = subprocess.Popen(
            [sys.executable, "-c", worker_script, "dying", "die"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        dying_output = dying_worker.communicate(timeout=30)
        self.assertEqual(dying_worker.returncode, 0)
        # The dying worker is now reaped (no zombie), and its lock is stale.

        # Now start 3 live workers that must reap the stale lock and continue.
        workers = [
            subprocess.Popen(
                [sys.executable, "-c", worker_script, str(i), "live"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            for i in range(3)
        ]
        outputs = [w.communicate(timeout=60) for w in workers]

        for i, (worker, output) in enumerate(zip(workers, outputs)):
            combined = "".join(output)
            self.assertEqual(
                worker.returncode, 0,
                f"Live worker {i} failed:\n{combined}",
            )

        # Verify the shared file has content from all workers
        content = shared_file.read_text(encoding="utf-8")
        lines = [l for l in content.strip().split("\n") if l]
        # dying worker × 3 iterations (0,1,2) + 3 live workers × 5 iterations
        self.assertEqual(len(lines), 18, f"Expected 18 lines, got {len(lines)}:\n{content}")


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

    def test_status_token_fifo_does_not_hang(self) -> None:
        lock_dir = self.path.parent / f"{self.path.name}.locket"
        lock_dir.mkdir()
        os.mkfifo(lock_dir / core.TOKENFILE_NAME)

        try:
            result = subprocess.run(
                [*CLI, "status", str(self.path)],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
                timeout=1,
            )
        except subprocess.TimeoutExpired:
            self.fail("status hung on a fifo token path")

        self.assertEqual(result.returncode, core.EXIT_BAD_LOCK)
        self.assertIn("Corrupt lock directory", result.stdout)
        self.assertEqual(result.stderr, "")

    def test_status_tag_fifo_does_not_hang(self) -> None:
        lock_dir = self.path.parent / f"{self.path.name}.locket"
        lock_dir.mkdir()
        (lock_dir / core.TOKENFILE_NAME).write_text("abcd1234\n", encoding="utf-8")
        os.mkfifo(lock_dir / core.TAGFILE_NAME)

        try:
            result = subprocess.run(
                [*CLI, "status", str(self.path)],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
                timeout=1,
            )
        except subprocess.TimeoutExpired:
            self.fail("status hung on a fifo tag path")

        self.assertEqual(result.returncode, core.EXIT_BAD_LOCK)
        self.assertIn("Corrupt lock directory", result.stdout)
        self.assertEqual(result.stderr, "")

    def test_status_with_naive_locked_at_reports_without_traceback(self) -> None:
        lock_dir = self.path.parent / f"{self.path.name}.locket"
        lock_dir.mkdir()
        (lock_dir / core.TOKENFILE_NAME).write_text("abcd1234\n", encoding="utf-8")
        (lock_dir / core.TAGFILE_NAME).write_text(
            '{"locked_at": "2026-03-18T12:34:56", "user": "me"}\n',
            encoding="utf-8",
        )

        result = self.run_cli("status", str(self.path))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Locked:", result.stdout)
        self.assertNotIn("Traceback", result.stderr)

    def test_status_unknown_user_home_reports_usage_without_traceback(self) -> None:
        result = self.run_cli(
            "status",
            "~definitely-no-such-user-12345/file.txt",
        )
        self.assertEqual(result.returncode, core.EXIT_USAGE)
        self.assertEqual(result.stdout, "")
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("home directory", result.stderr.lower())

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

    def test_with_lock_command_under_file_path_reports_error_without_traceback(self) -> None:
        not_a_directory = Path(self.tmpdir.name) / "not-a-directory"
        not_a_directory.write_text("hello\n", encoding="utf-8")
        command = not_a_directory / "child-command"

        result = self.run_cli("with-lock", str(self.path), "--", str(command))
        self.assertEqual(result.returncode, core.EXIT_IO)
        self.assertEqual(result.stdout, "")
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn(str(command), result.stderr)

        status = self.run_cli("status", str(self.path))
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        self.assertIn("Unlocked:", status.stdout)

    def test_with_lock_exec_format_error_reports_error_without_traceback(self) -> None:
        command = Path(self.tmpdir.name) / "plain-exec"
        command.write_text("echo hello\n", encoding="utf-8")
        command.chmod(0o755)

        result = self.run_cli("with-lock", str(self.path), "--", str(command))
        self.assertEqual(result.returncode, core.EXIT_IO)
        self.assertEqual(result.stdout, "")
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn(str(command), result.stderr)

        status = self.run_cli("status", str(self.path))
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        self.assertIn("Unlocked:", status.stdout)

    def test_with_lock_maps_signaled_child_exit_code_and_releases(self) -> None:
        import signal

        child = [
            sys.executable,
            "-c",
            "import os, signal; os.kill(os.getpid(), signal.SIGTERM)",
        ]

        result = self.run_cli("with-lock", str(self.path), "--", *child)

        self.assertEqual(
            result.returncode,
            128 + signal.SIGTERM,
            result.stdout + result.stderr,
        )

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
