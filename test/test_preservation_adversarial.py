"""Adversarial disposable-repository tests for trusted preservation.

These tests own only hostile confirmation, drift, process-isolation, and
partial-execution cases.  They use real Git repositories and the public host
interface; native UI results are injected only at the documented test seam.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import fcntl
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


HERE = Path(__file__).resolve().parent


def _trusted_tests():
    spec = importlib.util.spec_from_file_location("trusted_preservation_tests", HERE / "test_trusted_preservation.py")
    if spec is None or spec.loader is None:
        raise AssertionError("trusted preservation fixture helpers are unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PreservationAdversarialTest(unittest.TestCase):
    def test_native_result_parser_accepts_only_a_fixed_success_result(self) -> None:
        """Permissive osascript parsing could turn cancel, failure, or garbage into approval."""
        fixture = _trusted_tests()
        host = fixture.load_host()
        cases = (
            ("cancel", subprocess.CompletedProcess([host.OSASCRIPT], 0, "Cancel\n", "")),
            ("gave-up", subprocess.CompletedProcess([host.OSASCRIPT], 0, "gave up\n", "")),
            ("nonzero", subprocess.CompletedProcess([host.OSASCRIPT], 1, "Run once\n", "failed")),
            ("malformed", subprocess.CompletedProcess([host.OSASCRIPT], 0, "unexpected\n", "")),
        )
        for name, result in cases:
            with self.subTest(result=name), mock.patch.object(host.subprocess, "run", return_value=result):
                self.assertFalse(host.native_confirm("fixed prompt", 1))
        for name, error in (
            ("timeout", subprocess.TimeoutExpired(host.OSASCRIPT, 1)),
            ("error", OSError("dialog transport error")),
        ):
            with self.subTest(result=name), mock.patch.object(host.subprocess, "run", side_effect=error):
                self.assertFalse(host.native_confirm("fixed prompt", 1))

    def test_native_timeout_or_error_at_transaction_boundary_runs_no_git_mutation(self) -> None:
        """Letting a dialog transport failure continue would make an unapproved stash."""
        fixture = _trusted_tests()
        host = fixture.load_host()
        cases = (
            ("timeout", lambda _prompt: (_ for _ in ()).throw(subprocess.TimeoutExpired(host.OSASCRIPT, 1))),
            ("error", lambda _prompt: (_ for _ in ()).throw(OSError("dialog transport error"))),
        )
        for name, confirm in cases:
            with self.subTest(result=name), tempfile.TemporaryDirectory() as raw:
                repo, request = fixture.make_stash_request(Path(raw).resolve(), host)
                self.assertEqual(host.execute_request(request, confirm=confirm), {"status": "rejected"})
                self.assertEqual(fixture.git_optional(repo, "rev-parse", "--verify", "--quiet", "refs/stash"), "")

    def test_wait_time_metadata_index_refs_and_new_pending_path_drift_each_prevent_stash(self) -> None:
        """Dropping any snapshot field would let a changed repository execute a stale approval."""
        fixture = _trusted_tests()
        host = fixture.load_host()

        def change_metadata(repo):
            target = repo / "tracked.txt"
            os.chmod(target, 0o744)

        def change_index(repo):
            fixture.git(repo, "update-index", "--chmod=+x", "tracked.txt")

        def change_refs(repo):
            fixture.git(repo, "update-ref", "refs/heads/adversarial-drift", "HEAD")

        def add_pending(repo):
            (repo / "new-pending.txt").write_text("unexpected\n")

        for name, mutate in (
            ("metadata", change_metadata),
            ("index", change_index),
            ("refs", change_refs),
            ("new-pending-path", add_pending),
        ):
            with self.subTest(drift=name), tempfile.TemporaryDirectory() as raw:
                repo, request = fixture.make_stash_request(Path(raw).resolve(), host)
                result = host.execute_request(request, confirm=lambda _prompt: mutate(repo) or True)
                self.assertIn(result["status"], {"rejected", "rejected-drift"})
                self.assertEqual(fixture.git_optional(repo, "rev-parse", "--verify", "--quiet", "refs/stash"), "")

    def test_independent_process_calls_cannot_share_an_acceptance_or_mutate_twice(self) -> None:
        """Moving approval outside the executor would let two processes consume one decision."""
        fixture = _trusted_tests()
        host = fixture.load_host()
        child = """
import importlib.util, json, sys, time
from pathlib import Path
module_path = Path(sys.argv[1])
request_path, barrier, release = map(Path, sys.argv[2:5])
spec = importlib.util.spec_from_file_location('trusted_preservation_child', module_path)
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)
request = json.loads(request_path.read_text())
def wait_with_lock(_prompt):
    barrier.write_text('approval-waiting')
    deadline = time.monotonic() + 30
    while not release.exists() and time.monotonic() < deadline: time.sleep(0.02)
    return release.exists()
print(json.dumps(host.execute_request(request, confirm=wait_with_lock), sort_keys=True))
"""
        with tempfile.TemporaryDirectory() as raw:
            repo, request = fixture.make_stash_request(Path(raw).resolve(), host)
            barrier = Path(raw).resolve() / "approval-waiting"
            release = Path(raw).resolve() / "release-approval"; request_file = Path(raw).resolve() / "request.json"; request_file.write_text(json.dumps(request))
            first = contender = None
            try:
                first = subprocess.Popen([sys.executable, "-c", child, str(fixture.MODULE), str(request_file), str(barrier), str(release)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                deadline = time.monotonic() + 30
                while not barrier.exists() and time.monotonic() < deadline: time.sleep(0.02)
                self.assertTrue(barrier.exists(), "first executor did not enter approval wait holding the delivery lock")
                self.assertIsNone(first.poll(), "first executor exited before contender launch")
                contender = subprocess.Popen([sys.executable, "-c", child, str(fixture.MODULE), str(request_file), str(Path(raw) / "second"), str(release)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                contender_out, contender_err = contender.communicate(timeout=60)
                self.assertEqual(contender.returncode, 0, contender_err)
                release.touch()
                first_out, first_err = first.communicate(timeout=60)
                self.assertEqual(first.returncode, 0, first_err)
                results = [json.loads(first_out), json.loads(contender_out)]
                self.assertEqual(sum(result == {"status": "success"} for result in results), 1)
                self.assertEqual(len(fixture.git(repo, "reflog", "show", "--format=%H", "refs/stash").splitlines()), 1)
            finally:
                release.touch()
                for process in (first, contender):
                    if process is not None and process.poll() is None:
                        process.kill(); process.wait(timeout=30)

    def test_ambiguous_result_after_actual_git_start_is_partial_and_never_retries(self) -> None:
        """Retrying after a lost Git result could create a second preservation mutation."""
        fixture = _trusted_tests()
        host = fixture.load_host()
        with tempfile.TemporaryDirectory() as raw:
            repo, request = fixture.make_stash_request(Path(raw).resolve(), host)
            original_run = host.subprocess.run
            mutation_calls = []

            def ambiguous_after_start(argv, *args, **kwargs):
                result = original_run(argv, *args, **kwargs)
                if isinstance(argv, list) and "stash" in argv and "push" in argv:
                    mutation_calls.append(tuple(argv))
                    return subprocess.CompletedProcess(argv, 1, result.stdout, "lost result after child start")
                return result

            with mock.patch.object(host.subprocess, "run", side_effect=ambiguous_after_start):
                self.assertEqual(host.execute_request(request, confirm=lambda _prompt: True), {"status": "failed-or-partial"})
            self.assertEqual(len(mutation_calls), 1)
            self.assertEqual(len(fixture.git(repo, "reflog", "show", "--format=%H", "refs/stash").splitlines()), 1)

    def test_expiry_after_stash_prequery_refuses_before_actual_git_mutation(self) -> None:
        """Checking expiry only before operation prequeries would run Git after approval expired."""
        fixture = _trusted_tests()
        host = fixture.load_host()
        with tempfile.TemporaryDirectory() as raw:
            repo, request = fixture.make_stash_request(Path(raw).resolve(), host)
            request["expires_at"] = int(time.time()) + 10
            original_run_git = host.run_git

            def expire_after_old_stash_lookup(target, *args, **kwargs):
                result = original_run_git(target, *args, **kwargs)
                if args == ("rev-parse", "--verify", "--quiet", "refs/stash"):
                    time.sleep(11)
                return result

            with mock.patch.object(host, "run_git", side_effect=expire_after_old_stash_lookup):
                self.assertEqual(host.execute_request(request, confirm=lambda _prompt: True), {"status": "rejected-drift"})
            self.assertEqual(fixture.git_optional(repo, "rev-parse", "--verify", "--quiet", "refs/stash"), "")

    def test_replaced_policy_helper_during_wait_refuses_before_stash(self) -> None:
        """Binding only executable dependencies would let a changed source helper execute after approval."""
        fixture = _trusted_tests()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            copied_bin = root / "private-bin"
            copied_bin.mkdir()
            for name in ("trusted_preservation.py", "preservation_snapshot.py", "git_policy.py"):
                shutil.copy2(fixture.ROOT / "bin" / name, copied_bin / name)
            spec = importlib.util.spec_from_file_location("trusted_preservation_private_copy", copied_bin / "trusted_preservation.py")
            if spec is None or spec.loader is None:
                self.fail("private copied host fixture could not load")
            host = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(host)
            repo, request = fixture.make_stash_request(root, host)
            helper = copied_bin / "git_policy.py"

            def accept_then_replace_helper(_prompt: str) -> bool:
                helper.write_bytes(helper.read_bytes() + b"\n# altered after confirmation\n")
                return True

            self.assertEqual(host.execute_request(request, confirm=accept_then_replace_helper), {"status": "rejected-drift"})
            self.assertEqual(fixture.git_optional(repo, "rev-parse", "--verify", "--quiet", "refs/stash"), "")

    def test_parent_death_keeps_flock_until_the_actual_git_child_exits(self) -> None:
        """Closing the parent lock before its live Git child exits would admit a second executor."""
        fixture = _trusted_tests()
        host = fixture.load_host()
        executor = """
import importlib.util, json, os, signal, sys
from pathlib import Path
module_path, marker_path = map(Path, sys.argv[1:3])
spec = importlib.util.spec_from_file_location('trusted_preservation_executor', module_path)
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)
real_popen = host.subprocess.Popen
def stop_actual_mutation(*args, **kwargs):
    argv = args[0] if args else kwargs['args']
    child = real_popen(*args, **kwargs)
    if isinstance(argv, list) and 'stash' in argv and 'push' in argv:
        os.kill(child.pid, signal.SIGSTOP)
        marker_path.write_text(str(child.pid))
    return child
host.subprocess.Popen = stop_actual_mutation
print(json.dumps(host.execute_request(json.loads(sys.stdin.read()), confirm=lambda _prompt: True)))
"""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            repo, request = fixture.make_stash_request(root, host)
            marker = root / "stopped-git-pid"
            process = subprocess.Popen(
                [sys.executable, "-c", executor, str(fixture.MODULE), str(marker)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            child_pid = None
            try:
                assert process.stdin is not None
                process.stdin.write(json.dumps(request)); process.stdin.close()
                deadline = time.monotonic() + 30
                while not marker.is_file() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(marker.is_file(), "actual Git mutation child was not started")
                child_pid = int(marker.read_text())
                os.kill(process.pid, signal.SIGKILL)
                process.wait(timeout=30)
                lock = os.open(repo / ".git" / "harness-deliver.lock", os.O_RDWR)
                try:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(lock)
                os.kill(child_pid, signal.SIGCONT)
                release_deadline = time.monotonic() + 30
                while time.monotonic() < release_deadline:
                    lock = os.open(repo / ".git" / "harness-deliver.lock", os.O_RDWR)
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        time.sleep(0.02)
                    else:
                        fcntl.flock(lock, fcntl.LOCK_UN); break
                    finally:
                        os.close(lock)
                else:
                    self.fail("Git child did not release inherited flock")
            finally:
                if process.poll() is None:
                    os.kill(process.pid, signal.SIGKILL); process.wait(timeout=30)
                if child_pid is not None:
                    try:
                        os.kill(child_pid, signal.SIGCONT)
                    except ProcessLookupError:
                        pass
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
