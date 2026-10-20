"""Real-filesystem contract tests for pending isolated-session creation."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import importlib.util


ROOT = Path(__file__).resolve().parents[1]


def _creation_module():
    spec = importlib.util.spec_from_file_location("harness_session_creation", ROOT / "bin" / "harness_session_creation.py")
    if spec is None or spec.loader is None:
        raise AssertionError("creation module is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class SessionCreationContractTest(unittest.TestCase):
    def test_state_spelling_survives_parent_alias_for_legacy_consumers(self) -> None:
        """Legacy catalog compares the configured state prefix with session-root."""
        creation_module = _creation_module()
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            parent = base / 'physical'
            parent.mkdir()
            alias = base / 'alias'
            alias.symlink_to(parent, target_is_directory=True)
            state = alias / 'state'
            state.mkdir(mode=0o700)
            source = base / 'source'
            source.mkdir()
            with creation_module.Creation(state, source) as creation:
                self.assertEqual(creation.root, state / 'worktrees' / creation.id)
                report = creation_module.reconcile(state, source)
                self.assertEqual(report['outcomes'], [
                    {'id': creation.id, 'outcome': 'live', 'phase': 'reserved'}])

    def test_context_reserves_a_fresh_private_uuid_and_holds_an_os_lease(self) -> None:
        """Removing reservation-before-prepare must make a duplicate creator fail."""
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            source = Path(temporary) / "source"
            source.mkdir()
            program = """
import fcntl
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[3])
from harness_session_creation import Creation

state = Path(sys.argv[1])
source = Path(sys.argv[2])
pin = {
    'version': '0.69.0',
    'revision': 'a' * 40,
    'path': '/private/runtime/store/0.69.0',
    'manifest_sha256': 'b' * 64,
}
with Creation(state, source, pin) as creation:
    record = state / 'sessions' / creation.id
    assert creation.id
    assert record.is_dir()
    assert (record / 'pending').is_file()
    assert not (record / 'journal').exists()
    lease = record / 'creation.lock'
    assert lease.is_file()
    contender = open(lease, 'rb')
    try:
        try:
            fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            raise AssertionError('creation lease was not held')
    finally:
        contender.close()
"""
            environment = dict(os.environ)
            result = subprocess.run(
                [sys.executable, "-I", "-B", "-c", program, str(state), str(source), str(ROOT / "bin")],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_prepare_fences_legacy_journal_until_commit(self) -> None:
        """Publishing OPEN from the reserved engine before commit reopens old resume."""
        creation_module = _creation_module()
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source"
            state = base / "state"
            subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
            subprocess.run(["git", "-C", str(source), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(source), "config", "user.name", "test"], check=True)
            (source / "tracked.txt").write_text("seed\n")
            subprocess.run(["git", "-C", str(source), "add", "tracked.txt"], check=True)
            subprocess.run(["git", "-C", str(source), "commit", "-qm", "seed"], check=True)
            with creation_module.Creation(state, source) as creation:
                try:
                    fields = creation.prepare(ROOT / "bin" / "session-isolation.sh", dict(os.environ), local=True)
                except creation_module.CreationError as error:
                    self.fail(str(error))
                self.assertEqual(fields["HARNESS_SESSION_ID"], creation.id)
                self.assertFalse((state / "sessions" / creation.id / "journal").exists())
                creation.publication_started()
                creation.commit()
            self.assertIn("state=OPEN", (state / "sessions" / creation.id / "journal").read_text())

    def test_reconcile_reports_a_live_creation_lease_without_finalizing_it(self) -> None:
        """Treating a child-held creation lease as dead can delete live work."""
        creation_module = _creation_module()
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            source = Path(temporary) / "source"
            source.mkdir()
            with creation_module.Creation(state, source) as creation:
                report = creation_module.reconcile(state, source)
                self.assertEqual(report["outcomes"], [{"id": creation.id, "outcome": "live", "phase": "reserved"}])
                self.assertTrue((state / "sessions" / creation.id / "pending").exists())

    def test_unpublished_context_exit_removes_only_its_own_empty_reservation(self) -> None:
        """Leaving an abandoned private reservation must not leave an admission block."""
        creation_module = _creation_module()
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            source = Path(temporary) / "source"
            source.mkdir()
            with creation_module.Creation(state, source) as creation:
                record = creation.record
                self.assertTrue(record.is_dir())
            self.assertFalse(record.exists())

    def test_source_base_returns_the_exact_local_main_commit(self) -> None:
        """Selecting HEAD instead of local main changes an offline session base."""
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source"
            subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
            subprocess.run(["git", "-C", str(source), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(source), "config", "user.name", "test"], check=True)
            (source / "tracked.txt").write_text("seed\n")
            subprocess.run(["git", "-C", str(source), "add", "tracked.txt"], check=True)
            subprocess.run(["git", "-C", str(source), "commit", "-qm", "seed"], check=True)
            expected = subprocess.check_output(["git", "-C", str(source), "rev-parse", "main^{commit}"], text=True).strip()
            result = subprocess.run([sys.executable, "-I", "-B", str(ROOT / "bin" / "harness_session_creation.py"),
                                     "source-base", "--source", str(source), "--local"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), expected)

    def test_reconcile_does_not_starve_this_source_behind_foreign_pending_records(self) -> None:
        """Cutting the global directory listing before source filtering hides recoverable work."""
        creation_module = _creation_module()
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            source = Path(temporary) / "source"
            source.mkdir()
            state.mkdir(mode=0o700)
            sessions = state / "sessions"
            sessions.mkdir(mode=0o700)
            for number in range(128):
                foreign = sessions / f"00000000-0000-4000-8000-{number:012d}"
                foreign.mkdir()
                (foreign / "pending").touch()
            with creation_module.Creation(state, source) as creation:
                report = creation_module.reconcile(state, source)
                self.assertIn({"id": creation.id, "outcome": "live", "phase": "reserved"}, report["outcomes"])

    def test_reservation_rejects_a_symlinked_state_root(self) -> None:
        """Resolving a caller-supplied state symlink can reserve an external directory."""
        creation_module = _creation_module()
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source"
            outside = base / "outside"
            source.mkdir()
            outside.mkdir()
            state = base / "state"
            state.symlink_to(outside, target_is_directory=True)
            with self.assertRaises(creation_module.CreationError):
                with creation_module.Creation(state, source):
                    self.fail("symlinked state root was admitted")

    def test_source_base_waits_for_same_physical_repository_lock(self) -> None:
        """Removing the common-dir lock lets concurrent fetches choose different bases."""
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source"
            subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
            subprocess.run(["git", "-C", str(source), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(source), "config", "user.name", "test"], check=True)
            (source / "seed").write_text("seed\n")
            subprocess.run(["git", "-C", str(source), "add", "seed"], check=True)
            subprocess.run(["git", "-C", str(source), "commit", "-qm", "seed"], check=True)
            common = Path(subprocess.check_output(["git", "-C", str(source), "rev-parse", "--path-format=absolute", "--git-common-dir"], text=True).strip())
            holder = subprocess.Popen([sys.executable, "-I", "-B", "-c", "import fcntl,sys,time; f=open(sys.argv[1],'a+'); fcntl.flock(f,fcntl.LOCK_EX); print('held',flush=True); time.sleep(3)", str(common / "harness-session-fetch.lock")], stdout=subprocess.PIPE, text=True)
            self.assertEqual(holder.stdout.readline().strip(), "held")
            waiter = subprocess.Popen([sys.executable, "-I", "-B", str(ROOT / "bin" / "harness_session_creation.py"), "source-base", "--source", str(source), "--local"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            time.sleep(0.2)
            self.assertIsNone(waiter.poll(), "source-base bypassed an active common-dir lock")
            holder.wait(timeout=5)
            stdout, stderr = waiter.communicate(timeout=5)
            holder.stdout.close()
            self.assertEqual(waiter.returncode, 0, stderr)
            self.assertEqual(stdout.strip(), subprocess.check_output(["git", "-C", str(source), "rev-parse", "main^{commit}"], text=True).strip())

    def test_source_base_for_another_repository_does_not_wait_on_foreign_lock(self) -> None:
        """A global fetch lock serializes unrelated harness sources."""
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            sources = [base / "source-a", base / "source-b"]
            for source in sources:
                subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
                subprocess.run(["git", "-C", str(source), "config", "user.email", "test@example.invalid"], check=True)
                subprocess.run(["git", "-C", str(source), "config", "user.name", "test"], check=True)
                (source / "seed").write_text("seed\n")
                subprocess.run(["git", "-C", str(source), "add", "seed"], check=True)
                subprocess.run(["git", "-C", str(source), "commit", "-qm", "seed"], check=True)
            common = Path(subprocess.check_output(["git", "-C", str(sources[0]), "rev-parse", "--path-format=absolute", "--git-common-dir"], text=True).strip())
            holder = subprocess.Popen([sys.executable, "-I", "-B", "-c", "import fcntl,sys,time; f=open(sys.argv[1],'a+'); fcntl.flock(f,fcntl.LOCK_EX); print('held',flush=True); time.sleep(3)", str(common / "harness-session-fetch.lock")], stdout=subprocess.PIPE, text=True)
            self.assertEqual(holder.stdout.readline().strip(), "held")
            result = subprocess.run([sys.executable, "-I", "-B", str(ROOT / "bin" / "harness_session_creation.py"), "source-base", "--source", str(sources[1]), "--local"], capture_output=True, text=True, timeout=1)
            holder.terminate(); holder.wait(timeout=5); holder.stdout.close()
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_source_wait_diagnostic_precedes_the_first_retry_sleep(self) -> None:
        """A silent source-lock wait gives callers no bounded-phase diagnosis."""
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source"
            subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
            subprocess.run(["git", "-C", str(source), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(source), "config", "user.name", "test"], check=True)
            (source / "seed").write_text("seed\n")
            subprocess.run(["git", "-C", str(source), "add", "seed"], check=True)
            subprocess.run(["git", "-C", str(source), "commit", "-qm", "seed"], check=True)
            common = Path(subprocess.check_output(["git", "-C", str(source), "rev-parse", "--path-format=absolute", "--git-common-dir"], text=True).strip())
            holder = subprocess.Popen([sys.executable, "-I", "-B", "-c", "import fcntl,sys,time; f=open(sys.argv[1],'a+'); fcntl.flock(f,fcntl.LOCK_EX); print('held',flush=True); time.sleep(3)", str(common / "harness-session-fetch.lock")], stdout=subprocess.PIPE, text=True)
            self.assertEqual(holder.stdout.readline().strip(), "held")
            script = "import argparse,importlib.util,io,sys;spec=importlib.util.spec_from_file_location('creation',sys.argv[1]);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);capture=io.StringIO();m.sys.stderr=capture;m.time.sleep=lambda _: (print(capture.getvalue(),end=''),(_ for _ in ()).throw(SystemExit(75)))[1];m._source_base(argparse.Namespace(source=sys.argv[2],local=True))"
            result = subprocess.run([sys.executable, "-I", "-B", "-c", script, str(ROOT / "bin" / "harness_session_creation.py"), str(source)], capture_output=True, text=True)
            holder.terminate(); holder.wait(timeout=5); holder.stdout.close()
            self.assertEqual(result.returncode, 75, result.stderr)
            self.assertIn("phase=source_wait", result.stdout)


if __name__ == "__main__":
    unittest.main()
