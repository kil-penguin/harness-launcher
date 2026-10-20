"""Adversarial process and filesystem tests for pending session creation."""
from __future__ import annotations

import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import importlib.util
from unittest import mock


sys.dont_write_bytecode = True


ROOT = Path(__file__).resolve().parents[1]
CREATION = ROOT / "bin" / "harness_session_creation.py"


def _module():
    spec = importlib.util.spec_from_file_location("creation_adversarial", CREATION)
    if spec is None or spec.loader is None:
        raise AssertionError("creation helper is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _source(base: Path) -> Path:
    source = base / "source"
    subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(source), "config", "user.name", "test"], check=True)
    (source / "tracked.txt").write_text("seed\n")
    subprocess.run(["git", "-C", str(source), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-qm", "seed"], check=True)
    return source


def _eventually_missing(pid: int, timeout: float = 3) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.02)
    return False


def _kill_group_if_alive(pid: int) -> None:
    if _eventually_missing(pid, 0):
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        return


class SessionCreationAdversarialTest(unittest.TestCase):
    def test_stop_engine_does_not_signal_an_already_reaped_leader_group(self) -> None:
        """A PID whose leader is already reaped is not safe group-signal authority."""
        creation_module = _module()

        class ReapedLeader:
            pid = 424242
            returncode = 0

            def communicate(self, *, timeout: float):
                raise AssertionError("reaped leader must not be communicated again")

        child = ReapedLeader()
        with mock.patch.object(creation_module.os, "killpg") as killpg:
            creation_module._stop_engine(child)
        self.assertEqual(killpg.call_args_list, [])

    def test_keyboard_interrupt_reaps_reserved_engine_process_group(self) -> None:
        """Removing interrupt cleanup must leave the engine's descendant alive."""
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            state = base / "state"
            source = base / "source"
            source.mkdir()
            engine = base / "engine.py"
            engine.write_text(
                "#!/usr/bin/env python3\n"
                "import os, pathlib, subprocess, sys\n"
                "base = pathlib.Path(os.environ['TEST_EVENT_DIR'])\n"
                "child = subprocess.Popen([sys.executable, '-c', \"import pathlib,os,signal; pathlib.Path(os.environ['TEST_EVENT_DIR'], 'child.ready').write_text(str(os.getpid())); signal.pause()\"], env=os.environ.copy())\n"
                "base.joinpath('engine.ready').write_text(str(os.getpid()))\n"
                "sys.stdout.flush()\n"
                "import signal; signal.pause()\n"
            )
            engine.chmod(0o700)
            caller = base / "caller.py"
            caller.write_text(
                "import importlib.util, os, sys\n"
                "from pathlib import Path\n"
                "spec = importlib.util.spec_from_file_location('creation', sys.argv[1])\n"
                "module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)\n"
                "with module.Creation(Path(sys.argv[2]), Path(sys.argv[3])) as creation:\n"
                "    creation.prepare(Path(sys.argv[4]), dict(os.environ), local=True, timeout=30)\n"
            )
            environment = dict(os.environ, TEST_EVENT_DIR=str(base))
            process = subprocess.Popen(
                [sys.executable, "-I", "-B", str(caller), str(CREATION), str(state), str(source), str(engine)],
                env=environment,
                start_new_session=True,
            )
            self.addCleanup(lambda: process.poll() is None and os.killpg(process.pid, signal.SIGKILL))
            deadline = time.monotonic() + 5
            while not (base / "child.ready").exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue((base / "engine.ready").exists(), "reserved engine did not reach barrier")
            child_pid = int((base / "child.ready").read_text())
            engine_pid = int((base / "engine.ready").read_text())
            os.killpg(process.pid, signal.SIGINT)
            self.assertIsNotNone(process.wait(timeout=5))
            self.assertTrue(_eventually_missing(engine_pid), "interrupt left reserved engine alive")
            self.assertTrue(_eventually_missing(child_pid), "interrupt left engine descendant alive")

    def test_restore_boundary_interrupt_restores_prior_handler_and_preserves_creation(self) -> None:
        """An interrupt while restoring SIGINT must not leak the private cancellation type."""
        creation_module = _module()
        original_signal = creation_module.signal.signal
        prior_handler = creation_module.signal.getsignal(signal.SIGINT)
        calls = 0

        def interrupt_restore(signum: int, handler):
            nonlocal calls
            calls += 1
            if calls >= 2:
                os.kill(os.getpid(), signal.SIGINT)
            return original_signal(signum, handler)

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source"
            source.mkdir()
            engine = base / "engine.sh"
            engine.write_text("#!/bin/sh\nexit 2\n")
            engine.chmod(0o700)
            creation = creation_module.Creation(base / "state", source)
            try:
                with mock.patch.object(creation_module.signal, "signal", side_effect=interrupt_restore):
                    with self.assertRaises(KeyboardInterrupt):
                        with creation:
                            creation.prepare(engine, dict(os.environ), local=True, timeout=5)
            finally:
                original_signal(signal.SIGINT, prior_handler)
            self.assertEqual(calls, 3)
            self.assertIs(creation_module.signal.getsignal(signal.SIGINT), prior_handler)
            self.assertTrue(creation._preserve)

    def test_second_interrupt_reaps_a_term_ignoring_engine_group(self) -> None:
        """A second interrupt must finish bounded reaping after the TERM barrier."""
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            state = base / "state"
            source = base / "source"
            source.mkdir()
            engine = base / "engine.py"
            engine.write_text(
                "#!/usr/bin/env python3\n"
                "import os, pathlib, signal\n"
                "base = pathlib.Path(os.environ['TEST_EVENT_DIR'])\n"
                "base.joinpath('engine.ready').write_text(str(os.getpid()))\n"
                "def ignore_term(_signum, _frame): base.joinpath('term.barrier').write_text('TERM')\n"
                "signal.signal(signal.SIGTERM, ignore_term)\n"
                "signal.pause()\n"
                "while True: signal.pause()\n"
            )
            engine.chmod(0o700)
            caller = base / "caller.py"
            caller.write_text(
                "import importlib.util, os, sys\n"
                "from pathlib import Path\n"
                "spec = importlib.util.spec_from_file_location('creation', sys.argv[1])\n"
                "module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)\n"
                "with module.Creation(Path(sys.argv[2]), Path(sys.argv[3])) as creation:\n"
                "    creation.prepare(Path(sys.argv[4]), dict(os.environ), local=True, timeout=30)\n"
            )
            caller_process = subprocess.Popen(
                [sys.executable, "-I", "-B", str(caller), str(CREATION), str(state), str(source), str(engine)],
                env=dict(os.environ, TEST_EVENT_DIR=str(base)), start_new_session=True,
            )
            self.addCleanup(lambda: caller_process.poll() is None and os.killpg(caller_process.pid, signal.SIGKILL))
            deadline = time.monotonic() + 5
            while not (base / "engine.ready").exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue((base / "engine.ready").exists(), "reserved engine did not reach barrier")
            engine_pid = int((base / "engine.ready").read_text())
            self.addCleanup(lambda: _kill_group_if_alive(engine_pid))
            os.killpg(caller_process.pid, signal.SIGINT)
            deadline = time.monotonic() + 5
            while not (base / "term.barrier").exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue((base / "term.barrier").exists(), "engine did not receive TERM")
            os.killpg(caller_process.pid, signal.SIGINT)
            caller_process.wait(timeout=12)
            self.assertTrue(_eventually_missing(engine_pid), "second interrupt left TERM-ignoring engine alive")

    def test_timeout_reaps_reserved_engine_process_group(self) -> None:
        """A timed-out reserved create must not leave its process group holding resources."""
        creation_module = _module()
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            state = base / "state"
            source = base / "source"
            source.mkdir()
            engine = base / "engine.py"
            engine.write_text(
                "#!/usr/bin/env python3\n"
                "import os, pathlib, subprocess, sys\n"
                "base = pathlib.Path(os.environ['TEST_EVENT_DIR'])\n"
                "child = subprocess.Popen([sys.executable, '-c', \"import pathlib,os,signal; pathlib.Path(os.environ['TEST_EVENT_DIR'], 'child.ready').write_text(str(os.getpid())); signal.pause()\"], env=os.environ.copy())\n"
                "base.joinpath('engine.ready').write_text(str(os.getpid()))\n"
                "import signal; signal.pause()\n"
            )
            engine.chmod(0o700)
            with creation_module.Creation(state, source) as creation:
                with self.assertRaisesRegex(creation_module.CreationError, "timed out"):
                    creation.prepare(engine, dict(os.environ, TEST_EVENT_DIR=str(base)), local=True, timeout=3)
            self.assertTrue((base / "engine.ready").exists(), "reserved engine did not reach barrier")
            engine_pid = int((base / "engine.ready").read_text())
            child_pid = int((base / "child.ready").read_text())
            self.assertTrue(_eventually_missing(engine_pid), "timeout left reserved engine alive")
            self.assertTrue(_eventually_missing(child_pid), "timeout left engine descendant alive")

    def test_timeout_permission_uncertainty_preserves_artifacts_and_keeps_timeout_primary(self) -> None:
        """EPERM during cleanup must retain live preparation and report the original timeout."""
        creation_module = _module()
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            state = base / "state"
            source = base / "source"
            source.mkdir()
            engine = base / "engine.py"
            engine.write_text(
                "#!/usr/bin/env python3\n"
                "import os, pathlib, signal\n"
                "pathlib.Path(os.environ['TEST_EVENT_DIR'], 'engine.ready').write_text(str(os.getpid()))\n"
                "signal.pause()\n"
            )
            engine.chmod(0o700)
            creation = creation_module.Creation(state, source)
            with mock.patch.object(creation_module.os, "killpg", side_effect=PermissionError(1, "EPERM")):
                with self.assertRaisesRegex(creation_module.CreationError, "timed out"):
                    with creation:
                        creation.prepare(engine, dict(os.environ, TEST_EVENT_DIR=str(base)), local=True, timeout=0.2)
            engine_pid = int((base / "engine.ready").read_text())
            self.addCleanup(lambda: _kill_group_if_alive(engine_pid))
            self.assertTrue(creation.record.is_dir(), "uncertain cleanup deleted its record")
            self.assertTrue(creation.root.is_dir(), "uncertain cleanup deleted its workspace")

    def test_timeout_with_unconfirmed_cleanup_preserves_without_an_inherited_lease(self) -> None:
        """The explicit unconfirmed flag, not a child lease, must retain this reservation."""
        creation_module = _module()

        class TimedOutChild:
            pid = 424242
            returncode = None

            def __init__(self, *_arguments, **_keywords):
                pass

            def communicate(self, *, timeout: float):
                raise subprocess.TimeoutExpired(["fake-engine"], timeout)

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            state = base / "state"
            source = base / "source"
            source.mkdir()
            creation = creation_module.Creation(state, source)
            with mock.patch.object(creation_module.subprocess, "Popen", TimedOutChild):
                with mock.patch.object(creation_module, "_stop_engine", return_value=False) as stop_engine:
                    with self.assertRaisesRegex(creation_module.CreationError, "timed out"):
                        with creation:
                            creation.prepare(ROOT / "bin" / "session-isolation.sh", dict(os.environ), local=True, timeout=0.1)
            stop_engine.assert_called_once()
            self.assertTrue(creation.record.is_dir(), "unconfirmed cleanup deleted its record without a child lease")
            self.assertTrue(creation.root.is_dir(), "unconfirmed cleanup deleted its workspace without a child lease")
            self.assertTrue(creation.index.is_file(), "unconfirmed cleanup removed its recovery index")

    def test_stop_engine_marks_zombie_eprem_as_unconfirmed(self) -> None:
        """EPERM from a zombie-only group is uncertainty, never authority for a retry."""
        creation_module = _module()

        class ZombieLeader:
            pid = 424242
            returncode = None

            def communicate(self, *, timeout: float):
                self.returncode = 0
                return "", ""

        child = ZombieLeader()
        with mock.patch.object(creation_module.os, "waitid", return_value=object()):
            with mock.patch.object(creation_module.os, "killpg", side_effect=PermissionError(1, "EPERM")) as killpg:
                self.assertFalse(creation_module._stop_engine(child))
        self.assertEqual(killpg.call_args_list, [mock.call(child.pid, signal.SIGTERM)])

    def test_leader_exit_with_closed_stdio_descendant_preserves_unconfirmed_reservation(self) -> None:
        """A lease-holding descendant after leader exit must retain the reservation for recovery."""
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            state = base / "state"
            source = base / "source"
            source.mkdir()
            engine = base / "engine.py"
            engine.write_text(
                "#!/usr/bin/env python3\n"
                "import os, pathlib, signal\n"
                "base = pathlib.Path(os.environ['TEST_EVENT_DIR'])\n"
                "if os.fork(): os._exit(0)\n"
                "base.joinpath('descendant.ready').write_text(f'{os.getpid()} {os.getpgrp()}')\n"
                "for descriptor in (0, 1, 2): os.close(descriptor)\n"
                "signal.pause()\n"
            )
            engine.chmod(0o700)
            caller = base / "caller.py"
            caller.write_text(
                "import importlib.util, os, sys\n"
                "from pathlib import Path\n"
                "spec = importlib.util.spec_from_file_location('creation', sys.argv[1])\n"
                "module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)\n"
                "with module.Creation(Path(sys.argv[2]), Path(sys.argv[3])) as creation:\n"
                "    creation.prepare(Path(sys.argv[4]), dict(os.environ), local=True, timeout=10)\n"
            )
            caller_process = subprocess.Popen(
                [sys.executable, "-I", "-B", str(caller), str(CREATION), str(state), str(source), str(engine)],
                env=dict(os.environ, TEST_EVENT_DIR=str(base)), start_new_session=True,
            )
            caller_process.wait(timeout=5)
            self.assertNotEqual(caller_process.returncode, 0, "leader exit must not produce valid creation fields")
            descendant_pid, process_group = map(int, (base / "descendant.ready").read_text().split())
            self.addCleanup(lambda: _kill_group_if_alive(process_group))
            record = next((state / "sessions").iterdir())
            root = next((state / "worktrees").iterdir())
            self.assertTrue(record.is_dir(), "descendant-held lease lost its record")
            self.assertTrue(root.is_dir(), "descendant-held lease lost its workspace")
            os.killpg(process_group, signal.SIGKILL)
            self.assertTrue(_eventually_missing(descendant_pid), "closed-stdio descendant was not reaped")

    def test_reserved_create_rejects_an_unlocked_descriptor_for_the_right_lock(self) -> None:
        """Dropping the inherited locked OFD must not authorize reserved create."""
        creation_module = _module()
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = _source(base)
            state = base / "state"
            with creation_module.Creation(state, source) as creation:
                engine = ROOT / "bin" / "session-isolation.sh"
                creation._write(engine_identity=creation_module._engine_identity(engine))
                wrong = os.open(creation.record / "creation.lock", os.O_RDWR)
                try:
                    result = subprocess.run(
                        [sys.executable, "-I", "-B", str(CREATION), "verify-reserved", "--record", str(creation.record),
                         "--source", str(source), "--engine", str(engine), "--lease-fd", str(wrong)],
                        pass_fds=(wrong,), capture_output=True, text=True,
                    )
                finally:
                    os.close(wrong)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("lease", result.stderr)

    def test_engine_child_holds_creation_lease_after_parent_sigkill(self) -> None:
        """Closing a parent must not make its inherited reserved engine look abandoned."""
        creation_module = _module()
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            state = base / "state"
            source = _source(base)
            engine = base / "engine.py"
            engine.write_text(
                "#!/usr/bin/env python3\n"
                "import os, pathlib, signal\n"
                "pathlib.Path(os.environ['TEST_EVENT_DIR'], 'engine.ready').write_text(str(os.getpid()))\n"
                "signal.pause()\n"
            )
            engine.chmod(0o700)
            caller = base / "caller.py"
            caller.write_text(
                "import importlib.util, os, sys\n"
                "from pathlib import Path\n"
                "spec = importlib.util.spec_from_file_location('creation', sys.argv[1])\n"
                "module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)\n"
                "with module.Creation(Path(sys.argv[2]), Path(sys.argv[3])) as creation:\n"
                "    creation.prepare(Path(sys.argv[4]), dict(os.environ), local=True, timeout=30)\n"
            )
            process = subprocess.Popen(
                [sys.executable, "-I", "-B", str(caller), str(CREATION), str(state), str(source), str(engine)],
                env=dict(os.environ, TEST_EVENT_DIR=str(base)), start_new_session=True,
            )
            deadline = time.monotonic() + 5
            while not (base / "engine.ready").exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue((base / "engine.ready").exists(), "reserved engine did not reach barrier")
            engine_pid = int((base / "engine.ready").read_text())
            self.addCleanup(lambda: _kill_group_if_alive(engine_pid))
            os.kill(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
            record = next((state / "sessions").iterdir())
            report = creation_module.reconcile(state, source)
            self.assertEqual(report["outcomes"], [{"id": record.name, "outcome": "live", "phase": "reserved"}])
            self.assertTrue((record / "pending").is_file())

    def test_context_exit_never_removes_a_replaced_reserved_worktree(self) -> None:
        """Cleanup without the reserved root inode check deletes a replacement directory."""
        creation_module = _module()
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            state = base / "state"
            source = base / "source"
            source.mkdir()
            with creation_module.Creation(state, source) as creation:
                creation.root.rmdir()
                creation.root.mkdir()
                sentinel = creation.root / "foreign-data"
                sentinel.write_text("must survive\n")
            self.assertEqual(sentinel.read_text(), "must survive\n")

    def test_replaced_prepared_record_cannot_publish_or_remove_foreign_data(self) -> None:
        """Replacing a record after prepare must fence both OPEN publication and cleanup."""
        creation_module = _module()
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = _source(base)
            state = base / "state"
            foreign = base / "foreign"
            foreign.mkdir()
            sentinel = foreign / "must-survive"
            sentinel.write_text("foreign\n")
            creation = creation_module.Creation(state, source)
            creation.__enter__()
            self.addCleanup(creation.__exit__)
            creation.prepare(ROOT / "bin" / "session-isolation.sh", dict(os.environ), local=True)
            displaced = creation.record.with_name("displaced-record")
            creation.record.rename(displaced)
            creation.record.symlink_to(foreign, target_is_directory=True)
            with self.assertRaises(creation_module.CreationError):
                creation.publication_started()
                creation.commit()
            creation.__exit__(None, None, None)
            self.assertEqual(sentinel.read_text(), "foreign\n")
            self.assertEqual(list(foreign.iterdir()), [sentinel])

    def test_reconcile_conflict_and_unknown_preserve_an_unadmitted_prepared_workspace(self) -> None:
        """Treating uncertain or conflicting bindings as absent deletes recoverable work."""
        creation_module = _module()
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = _source(base)
            pin = {"version": "0.69.0", "revision": "a" * 40, "path": str(ROOT), "manifest_sha256": "b" * 64}
            for status in ("CONFLICT", "UNKNOWN"):
                state = base / status.lower()
                creation = creation_module.Creation(state, source, pin)
                creation.__enter__()
                creation.prepare(ROOT / "bin" / "session-isolation.sh", dict(os.environ), local=True)
                lease = creation._lease
                assert lease is not None
                os.close(lease)
                creation._lease = None
                report = creation_module.reconcile(state, source, lambda _record: {"status": status})
                self.assertEqual(report["outcomes"], [{"id": creation.id, "outcome": "preserved", "phase": "prepared"}])
                self.assertTrue(creation.root.is_dir())
                self.assertFalse((creation.record / "journal").exists())

    def test_absent_reconcile_never_deletes_a_root_forged_in_pending_metadata(self) -> None:
        """Forged root path, identity, and session-root must not authorize foreign rmtree."""
        creation_module = _module()
        pin = {"version": "0.69.0", "revision": "a" * 40, "path": str(ROOT), "manifest_sha256": "b" * 64}
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = _source(base)
            state = base / "state"
            creation = creation_module.Creation(state, source, pin)
            creation.__enter__()
            creation.prepare(ROOT / "bin" / "session-isolation.sh", dict(os.environ), local=True)
            foreign = base / "foreign-root"
            foreign.mkdir()
            sentinel = foreign / "must-survive"
            sentinel.write_text("foreign\n")
            metadata = creation_module._read_json(creation.record / "creation.json")
            metadata["worktree_path"] = str(foreign)
            metadata["worktree_identity"] = creation_module._identity(foreign)
            metadata["worktree_parent_identity"] = creation_module._identity(foreign.parent)
            creation_module._atomic_json(creation.record / "creation.json", metadata)
            (creation.record / "session-root").write_text(str(foreign) + "\n")
            lease = creation._lease
            assert lease is not None
            os.close(lease)
            creation._lease = None
            report = creation_module.reconcile(state, source, lambda _record: {"status": "ABSENT"})
            self.assertEqual(report["outcomes"], [{"id": creation.id, "outcome": "invalid", "phase": "unknown"}])
            self.assertEqual(sentinel.read_text(), "foreign\n")
            self.assertTrue(creation.record.is_dir())

    def test_reconcile_rejects_a_replaced_creation_lock_while_original_lease_is_live(self) -> None:
        """A replacement lock pathname must not hide the live original flock descriptor."""
        creation_module = _module()
        pin = {"version": "0.69.0", "revision": "a" * 40, "path": str(ROOT), "manifest_sha256": "b" * 64}
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = _source(base)
            state = base / "state"
            with creation_module.Creation(state, source, pin) as creation:
                creation.prepare(ROOT / "bin" / "session-isolation.sh", dict(os.environ), local=True)
                lock = creation.record / "creation.lock"
                lock.unlink()
                lock.touch(mode=0o600)
                report = creation_module.reconcile(state, source, lambda _record: {"status": "ABSENT"})
                self.assertEqual(report["outcomes"], [{"id": creation.id, "outcome": "invalid", "phase": "unknown"}])
                self.assertTrue(creation.root.is_dir())
                self.assertTrue((creation.record / "pending").is_file())

    def test_corrupt_indexed_pending_record_is_reported_preserved_and_does_not_block_new_creation(self) -> None:
        """Unreadable pending metadata must be retained visibly without blocking a new UUID."""
        creation_module = _module()
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source"
            source.mkdir()
            state = base / "state"
            corrupt = creation_module.Creation(state, source)
            corrupt.__enter__()
            (corrupt.record / "creation.json").write_text("{invalid json\n")
            lease = corrupt._lease
            assert lease is not None
            os.close(lease)
            corrupt._lease = None
            report = creation_module.reconcile(state, source)
            self.assertEqual(report["outcomes"], [{"id": corrupt.id, "outcome": "invalid", "phase": "unknown"}])
            self.assertTrue(corrupt.record.is_dir())
            self.assertTrue(corrupt.root.is_dir())
            with creation_module.Creation(state, source) as fresh:
                self.assertNotEqual(fresh.id, corrupt.id)
                self.assertTrue(fresh.record.is_dir())

    def test_enter_metadata_write_failure_releases_the_creation_flock(self) -> None:
        """An __enter__ exception must not strand the OS lease before __exit__ can run."""
        creation_module = _module()
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            state = base / "state"
            source = base / "source"
            source.mkdir()
            creation = creation_module.Creation(state, source)
            original_atomic_json = creation_module._atomic_json

            def fail_metadata_write(_path: Path, _value: dict[str, object]) -> None:
                raise OSError("injected metadata write failure")

            creation_module._atomic_json = fail_metadata_write
            try:
                with self.assertRaisesRegex(OSError, "injected metadata"):
                    creation.__enter__()
            finally:
                creation_module._atomic_json = original_atomic_json
            probe = subprocess.run(
                [sys.executable, "-I", "-B", "-c",
                 "import fcntl, os, sys; fd=os.open(sys.argv[1], os.O_RDWR); fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)",
                 str(creation.record / "creation.lock")],
                capture_output=True,
                text=True,
            )
            self.assertEqual(probe.returncode, 0, probe.stderr)
            creation.__exit__(None, None, None)

    def test_match_recovery_publishes_once_and_is_idempotent(self) -> None:
        """A verified binding after a lost response must publish exactly one normal journal."""
        creation_module = _module()
        pin = {"version": "0.69.0", "revision": "a" * 40, "path": str(ROOT), "manifest_sha256": "b" * 64}
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = _source(base)
            state = base / "state"
            creation = creation_module.Creation(state, source, pin)
            creation.__enter__()
            creation.prepare(ROOT / "bin" / "session-isolation.sh", dict(os.environ), local=True)
            creation.publication_started()
            lease = creation._lease
            assert lease is not None
            os.close(lease)
            creation._lease = None
            readback = {"status": "MATCH", "source_root": str(source.resolve()), "runtime": pin}
            recovered = creation_module.reconcile(state, source, lambda _record: readback)
            self.assertEqual(recovered["outcomes"], [{"id": creation.id, "outcome": "committed", "phase": "committed"}])
            self.assertIn("state=OPEN", (creation.record / "journal").read_text())
            again = creation_module.reconcile(state, source, lambda _record: readback)
            self.assertEqual(again["outcomes"], [])

    def test_commit_rejects_tampered_preparation_evidence_before_open(self) -> None:
        """Garbage preparation files or a changed prepared worktree must fence OPEN."""
        creation_module = _module()
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            for name in ("base-sha", "root-inode", "lease-v1"):
                with self.subTest(evidence=name):
                    source = _source(base / name)
                    state = base / f"state-{name}"
                    creation = creation_module.Creation(state, source)
                    creation.__enter__()
                    creation.prepare(ROOT / "bin" / "session-isolation.sh", dict(os.environ), local=True)
                    (creation.record / name).write_text("garbage\n")
                    creation.publication_started()
                    with self.assertRaises(creation_module.PublicationError):
                        creation.commit()
                    self.assertFalse((creation.record / "journal").exists())
                    creation.__exit__(None, None, None)
            source = _source(base / "worktree-head")
            (source / "tracked.txt").write_text("second base\n")
            subprocess.run(["git", "-C", str(source), "commit", "-am", "second base"], check=True)
            state = base / "state-worktree-head"
            creation = creation_module.Creation(state, source)
            creation.__enter__()
            creation.prepare(ROOT / "bin" / "session-isolation.sh", dict(os.environ), local=True)
            subprocess.run(["git", "-C", str(creation.root), "checkout", "-q", "--detach", "HEAD~1"], check=True)
            creation.publication_started()
            with self.assertRaises(creation_module.PublicationError):
                creation.commit()
            self.assertFalse((creation.record / "journal").exists())
            creation.__exit__(None, None, None)

    def test_commit_rejects_an_engine_hash_change_before_open(self) -> None:
        """The prepared engine hash must still name the executable at publication time."""
        creation_module = _module()
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            runtime = base / "runtime"
            runtime_bin = runtime / "bin"
            runtime_bin.mkdir(parents=True)
            for filename in ("session-isolation.sh", "harness_session_creation.py", "harness-common.sh"):
                shutil.copy2(ROOT / "bin" / filename, runtime_bin / filename)
            source = _source(base / "engine")
            state = base / "state-engine"
            engine = runtime_bin / "session-isolation.sh"
            creation = creation_module.Creation(state, source)
            creation.__enter__()
            creation.prepare(engine, dict(os.environ), local=True)
            engine.write_text(engine.read_text() + "\n# changed after prepare\n")
            creation.publication_started()
            with self.assertRaises(creation_module.PublicationError):
                creation.commit()
            self.assertFalse((creation.record / "journal").exists())
            creation.__exit__(None, None, None)

    def test_legacy_world_readable_state_containers_admit_new_private_records(self) -> None:
        """Existing 0755 state parents from public create must remain compatible."""
        creation_module = _module()
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = _source(base)
            state = base / "state"
            for name in ("sessions", "worktrees"):
                container = state / name
                container.mkdir(parents=True)
                container.chmod(0o755)
            with creation_module.Creation(state, source) as creation:
                self.assertEqual(creation.record.stat().st_mode & 0o777, 0o700)
                self.assertEqual(creation.root.stat().st_mode & 0o777, 0o700)

    def test_standalone_publication_intent_recovers_without_a_binding_lookup(self) -> None:
        """A standalone OPEN response loss must recover from prepared engine evidence alone."""
        creation_module = _module()
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = _source(base)
            state = base / "state"
            creation = creation_module.Creation(state, source)
            creation.__enter__()
            creation.prepare(ROOT / "bin" / "session-isolation.sh", dict(os.environ), local=True)
            creation.publication_started()
            lease = creation._lease
            assert lease is not None
            os.close(lease)
            creation._lease = None
            recovered = creation_module.reconcile(state, source)
            self.assertEqual(recovered["outcomes"], [{"id": creation.id, "outcome": "committed", "phase": "committed"}])
            self.assertIn("state=OPEN", (creation.record / "journal").read_text())
            self.assertEqual(creation_module.reconcile(state, source)["outcomes"], [])

    def test_absent_bound_creation_waits_for_runtime_lease_then_cleans_owned_artifacts(self) -> None:
        """ABSENT may clean only after a separate POSIX runtime lease holder exits."""
        creation_module = _module()
        pin = {"version": "0.69.0", "revision": "a" * 40, "path": str(ROOT), "manifest_sha256": "b" * 64}
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = _source(base)
            state = base / "state"
            creation = creation_module.Creation(state, source, pin)
            creation.__enter__()
            creation.prepare(ROOT / "bin" / "session-isolation.sh", dict(os.environ), local=True)
            creation.publication_started()
            lease = creation._lease
            assert lease is not None
            os.close(lease)
            creation._lease = None
            ready = base / "runtime-lease.ready"
            holder = subprocess.Popen(
                [sys.executable, "-I", "-B", "-c",
                 "import fcntl, os, pathlib, signal, sys; fd=os.open(sys.argv[1], os.O_RDWR); fcntl.lockf(fd, fcntl.LOCK_EX); pathlib.Path(sys.argv[2]).write_text(str(os.getpid())); signal.pause()",
                 str(creation.record / "runtime.lock"), str(ready)],
                start_new_session=True,
            )
            self.addCleanup(lambda: holder.poll() is None and os.killpg(holder.pid, signal.SIGKILL))
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(ready.exists(), "runtime lease holder did not reach barrier")
            absent = lambda _record: {"status": "ABSENT"}
            live = creation_module.reconcile(state, source, absent)
            self.assertEqual(live["outcomes"], [{"id": creation.id, "outcome": "preserved", "phase": "publication_started"}])
            self.assertTrue(creation.record.is_dir())
            self.assertTrue(creation.root.is_dir())
            self.assertTrue(creation.index.is_file())
            os.killpg(holder.pid, signal.SIGKILL)
            holder.wait(timeout=5)
            cleaned = creation_module.reconcile(state, source, absent)
            self.assertTrue(cleaned["outcomes"], "ABSENT recovery must report a disposition")
            self.assertFalse(creation.record.exists())
            self.assertFalse(creation.root.exists())
            self.assertFalse(creation.index.exists())

    def test_journal_publication_survives_a_lost_caller_response(self) -> None:
        """A lost reply after journal durability must finalize without rewriting the journal."""
        creation_module = _module()
        pin = {"version": "0.69.0", "revision": "a" * 40, "path": str(ROOT), "manifest_sha256": "b" * 64}
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = _source(base)
            state = base / "state"
            creation = creation_module.Creation(state, source, pin)
            original_open_journal = creation_module._open_journal

            def lose_acknowledgement(record: Path) -> None:
                original_open_journal(record)
                raise OSError("lost acknowledgement")

            creation_module._open_journal = lose_acknowledgement
            try:
                with creation:
                    creation.prepare(ROOT / "bin" / "session-isolation.sh", dict(os.environ), local=True)
                    creation.publication_started()
                    readback = {"status": "MATCH", "source_root": str(source.resolve()), "runtime": pin}
                    with self.assertRaisesRegex(OSError, "lost acknowledgement"):
                        creation.commit(readback)
            finally:
                creation_module._open_journal = original_open_journal
            journal = creation.record / "journal"
            before = journal.read_bytes()
            before_mtime = journal.stat().st_mtime_ns
            readback = {"status": "MATCH", "source_root": str(source.resolve()), "runtime": pin}
            report = creation_module.reconcile(state, source, lambda _record: readback)
            self.assertEqual(report["outcomes"], [{"id": creation.id, "outcome": "committed", "phase": "committed"}])
            self.assertEqual(journal.read_bytes(), before)
            self.assertEqual(journal.stat().st_mtime_ns, before_mtime)
            self.assertFalse(creation.index.exists())
            self.assertFalse((creation.record / "pending").exists())
            self.assertEqual(creation_module.reconcile(state, source, lambda _record: readback)["outcomes"], [])

    def test_match_recovery_completes_after_interrupted_journal_temp_write(self) -> None:
        """A stale pre-rename journal temp must not permanently block MATCH recovery."""
        creation_module = _module()
        pin = {"version": "0.69.0", "revision": "a" * 40, "path": str(ROOT), "manifest_sha256": "b" * 64}
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = _source(base)
            state = base / "state"
            creation = creation_module.Creation(state, source, pin)
            creation.__enter__()
            creation.prepare(ROOT / "bin" / "session-isolation.sh", dict(os.environ), local=True)
            creation.publication_started()
            (creation.record / ".journal.pending").write_text("interrupted writer\n")
            lease = creation._lease
            assert lease is not None
            os.close(lease)
            creation._lease = None
            readback = {"status": "MATCH", "source_root": str(source.resolve()), "runtime": pin}
            report = creation_module.reconcile(state, source, lambda _record: readback)
            self.assertEqual(report["outcomes"], [{"id": creation.id, "outcome": "committed", "phase": "committed"}])
            self.assertIn("state=OPEN", (creation.record / "journal").read_text())


if __name__ == "__main__":
    unittest.main()
