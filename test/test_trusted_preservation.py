"""Disposable-repository contract tests for the trusted preservation host.

Break protected: removing the pre-execution snapshot comparison would let an
accepted action mutate a repository after it drifted while the native dialog
was open.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "bin" / "trusted_preservation.py"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["/usr/bin/git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
    ).stdout.strip()


def git_optional(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["/usr/bin/git", "-C", str(repo), *args], capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin"},
    ).stdout.strip()


def load_host():
    if not MODULE.is_file():
        raise AssertionError("trusted preservation host is not implemented")
    spec = importlib.util.spec_from_file_location("trusted_preservation", MODULE)
    if spec is None or spec.loader is None:
        raise AssertionError("trusted preservation host is not implemented")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_stash_request(raw: Path, host):
    raw = raw.resolve()
    repo = raw / "repo"
    remote = raw / "origin.git"
    subprocess.run(["/usr/bin/git", "init", "--bare", str(remote)], check=True, capture_output=True)
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Test")
    (repo / "tracked.txt").write_text("base\n")
    git(repo, "add", "tracked.txt")
    git(repo, "commit", "-m", "base")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-u", "origin", "main")
    (repo / "tracked.txt").write_text("pending\n")
    snapshot = host.capture_snapshot(repo)
    return repo, {
        "version": 1, "operation": "stash-paths", "repo": str(repo),
        "expected_head": git(repo, "rev-parse", "HEAD"),
        "expected_remote": git(repo, "rev-parse", "refs/remotes/origin/main"),
        "snapshot_sha256": snapshot["sha256"], "evidence": [],
        "expires_at": int(time.time()) + 60, "session": "test", "paths": ["tracked.txt"],
    }


class TrustedPreservationTest(unittest.TestCase):
    def test_ssh_binding_rejects_unsafe_identity_boundaries(self) -> None:
        """Removing any location, file, ancestor, or strict-object check would admit an unsafe identity."""
        host = load_host()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve(); repo, _request = make_stash_request(root, host)
            common = repo / ".git"
            key = root / "key"; key.write_text("synthetic\n"); key.chmod(0o600)
            binding = {"hostname": "github.com", "user": "git", "identity_file": str(key)}
            for changes in ({"hostname": "other.invalid"}, {"user": "other"}, {"extra": True}, {"identity_file": "relative"}):
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    host._ssh_binding(binding | changes, repo, common)
            with self.assertRaises(ValueError):
                host._ssh_binding(binding, repo, common, [{"path": str(key)}])
            inside = repo / "key"; inside.write_text("synthetic\n"); inside.chmod(0o600)
            external_common = root / "common"; external_common.mkdir()
            common_key = external_common / "key"; common_key.write_text("synthetic\n"); common_key.chmod(0o600)
            link = root / "symlink"; link.symlink_to(key)
            hard = root / "hard"; os.link(key, hard)
            percent = root / "%h"; percent.write_text("synthetic\n"); percent.chmod(0o600)
            unsafe = root / "unsafe"; unsafe.mkdir(); unsafe.chmod(0o777)
            unsafe_key = unsafe / "key"; unsafe_key.write_text("synthetic\n"); unsafe_key.chmod(0o600)
            mode_key = root / "mode-key"; mode_key.write_text("synthetic\n"); mode_key.chmod(0o640)
            for candidate in (inside, common_key, link, hard, percent, unsafe_key, mode_key):
                with self.subTest(candidate=candidate.name), self.assertRaises(ValueError):
                    host._ssh_binding(binding | {"identity_file": str(candidate)}, repo, external_common)

    def test_ssh_drift_at_each_final_boundary_rejects_without_stash(self) -> None:
        """An ancestor change or late key change must not survive the final metadata checks."""
        host = load_host()
        for phase in ("confirmation-ancestor", "pre-mutation-key"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as raw:
                root = Path(raw).resolve(); repo, request = make_stash_request(root, host)
                directory = root / "identities"; directory.mkdir(mode=0o700)
                key = directory / "key"; key.write_text("synthetic\n"); key.chmod(0o600)
                git(repo, "remote", "set-url", "origin", "git@reviewed-alias:owner/repository.git")
                request.update({"snapshot_sha256": host.capture_snapshot(repo)["sha256"], "ssh": {"hostname": "github.com", "user": "git", "identity_file": str(key)}})
                queries = []
                def remote(*_args):
                    queries.append(True)
                    if len(queries) == 2 and phase == "pre-mutation-key": key.chmod(0o640)
                    return request["expected_remote"]
                def confirm(_prompt):
                    if phase == "confirmation-ancestor": directory.chmod(0o750)
                    return True
                with mock.patch.object(host, "_live_remote", side_effect=remote):
                    result = host.execute_request(request, confirm=confirm)
                self.assertEqual(result, {"status": "rejected-drift"})
                self.assertEqual(git_optional(repo, "rev-parse", "--verify", "--quiet", "refs/stash"), "")

    def test_confirmation_cannot_move_identity_into_reader_paths(self) -> None:
        """Checking identity after readers would expose a moved key before rejecting drift."""
        host = load_host()
        for destination in ("evidence", "pending"):
            with self.subTest(destination=destination), tempfile.TemporaryDirectory() as raw:
                root = Path(raw).resolve(); repo, request = make_stash_request(root, host)
                key = root / "dedicated-key"; key.write_text("synthetic secret\n"); key.chmod(0o600)
                identity = (key.stat().st_dev, key.stat().st_ino)
                evidence = root / "review.txt"; evidence.write_text("review\n"); evidence.chmod(0o600)
                request["evidence"] = [{"path": str(evidence), "sha256": hashlib.sha256(b"review\n").hexdigest()}]
                git(repo, "remote", "set-url", "origin", "git@reviewed-alias:owner/repository.git")
                request.update({"snapshot_sha256": host.capture_snapshot(repo)["sha256"], "ssh": {"hostname": "github.com", "user": "git", "identity_file": str(key)}})
                reads = []; original = Path.read_bytes
                def guarded_read(path):
                    info = path.stat()
                    if (info.st_dev, info.st_ino) == identity: reads.append(str(path))
                    return original(path)
                def confirm(_prompt):
                    key.replace(evidence if destination == "evidence" else repo / "tracked.txt")
                    return True
                with mock.patch.object(host, "_live_remote", return_value=request["expected_remote"]), mock.patch.object(Path, "read_bytes", guarded_read):
                    result = host.execute_request(request, confirm=confirm)
                self.assertEqual(result, {"status": "rejected-drift"})
                self.assertEqual(reads, [], "Python must not read the synthetic identity")
                self.assertEqual(git_optional(repo, "rev-parse", "--verify", "--quiet", "refs/stash"), "")

    def test_ssh_binding_records_metadata_without_reading_the_private_key(self) -> None:
        """Hashing or reading a private key would expose credential material to Python."""
        host = load_host()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve(); repo, request = make_stash_request(root, host)
            key = root / "dedicated-key"; key.write_text("private material must stay unread\n"); key.chmod(0o600)
            request["ssh"] = {"hostname": "github.com", "user": "git", "identity_file": str(key)}
            common = Path(git(repo, "rev-parse", "--git-common-dir"))
            common = common if common.is_absolute() else repo / common
            with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("private key read")):
                binding = host._ssh_binding(request["ssh"], repo, common.resolve())
            self.assertEqual(binding["identity_file"], str(key))
            self.assertEqual(binding["hostname"], "github.com")
            self.assertEqual(binding["user"], "git")

    def test_ssh_request_binds_the_live_query_and_native_prompt(self) -> None:
        """Dropping the request binding would silently use the default SSH authentication path."""
        host = load_host()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve(); repo, request = make_stash_request(root, host)
            key = root / "dedicated-key"; key.write_text("fixture key\n"); key.chmod(0o600)
            git(repo, "remote", "set-url", "origin", "git@reviewed-alias:owner/repository.git")
            snapshot = host.capture_snapshot(repo)
            request.update({"snapshot_sha256": snapshot["sha256"], "ssh": {"hostname": "github.com", "user": "git", "identity_file": str(key)}})
            prompts = []
            with mock.patch.object(host, "_live_remote", return_value=request["expected_remote"]) as live:
                result = host.execute_request(request, confirm=lambda prompt: prompts.append(prompt) or True)
            self.assertEqual(result, {"status": "success"})
            self.assertEqual(live.call_args.args[2], request["ssh"])
            self.assertIn("ssh-binding=", prompts[0])

    def test_ssh_identity_drift_during_confirmation_rejects_before_mutation(self) -> None:
        """Changing a reviewed identity after the dialog must not retain approval for its replacement."""
        host = load_host()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve(); repo, request = make_stash_request(root, host)
            key = root / "dedicated-key"; key.write_text("fixture key\n"); key.chmod(0o600)
            git(repo, "remote", "set-url", "origin", "git@reviewed-alias:owner/repository.git")
            request.update({"snapshot_sha256": host.capture_snapshot(repo)["sha256"], "ssh": {"hostname": "github.com", "user": "git", "identity_file": str(key)}})
            with mock.patch.object(host, "_live_remote", return_value=request["expected_remote"]):
                result = host.execute_request(request, confirm=lambda _prompt: key.chmod(0o640) or True)
            self.assertEqual(result, {"status": "rejected-drift"})
            self.assertEqual(git_optional(repo, "rev-parse", "--verify", "--quiet", "refs/stash"), "")

    def test_fsmonitor_callback_config_is_rejected_before_confirmation(self) -> None:
        """A repository-configured fsmonitor program must never run before the decision."""
        host = load_host()
        with tempfile.TemporaryDirectory() as raw:
            repo, request = make_stash_request(Path(raw), host)
            marker = Path(raw).resolve() / "callback-ran"
            git(repo, "config", "core.fsmonitor", f"/bin/sh -c 'touch {marker}'")
            prompted = []
            self.assertEqual(host.execute_request(request, confirm=lambda _prompt: prompted.append(True) or True), {"status": "rejected"})
            self.assertEqual(prompted, [])
            self.assertFalse(marker.exists())

    def test_replaced_lock_path_during_confirmation_rejects_before_mutation(self) -> None:
        """Keeping only the old lock FD would let a replacement pathname admit another executor."""
        host = load_host()
        with tempfile.TemporaryDirectory() as raw:
            repo, request = make_stash_request(Path(raw), host)
            lock = repo / ".git" / "harness-deliver.lock"
            def accept_then_replace(_prompt: str) -> bool:
                lock.rename(lock.with_name("old-lock")); lock.touch(); lock.chmod(0o600)
                return True
            self.assertEqual(host.execute_request(request, confirm=accept_then_replace), {"status": "rejected-drift"})
            self.assertEqual(git_optional(repo, "rev-parse", "--verify", "--quiet", "refs/stash"), "")

    def test_repository_symlink_ancestor_is_rejected_before_confirmation(self) -> None:
        """Resolving a symlinked parent would silently redirect the approved repository."""
        host = load_host()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); repo, request = make_stash_request(root, host)
            parent = root / "parent-link"; parent.symlink_to(root, target_is_directory=True)
            request["repo"] = str(parent / "repo")
            prompted = []
            self.assertEqual(host.execute_request(request, confirm=lambda _prompt: prompted.append(True) or True), {"status": "rejected"})
            self.assertEqual(prompted, [])
            self.assertEqual(git_optional(repo, "rev-parse", "--verify", "--quiet", "refs/stash"), "")

    def test_malformed_path_and_boolean_version_are_rejected_without_crashing(self) -> None:
        """Coercing JSON booleans or path objects could bypass the strict request boundary."""
        host = load_host()
        with tempfile.TemporaryDirectory() as raw:
            repo, request = make_stash_request(Path(raw), host)
            for field, value in (("version", True), ("paths", [{}])):
                malformed = dict(request); malformed[field] = value
                self.assertEqual(host.execute_request(malformed, confirm=lambda _prompt: True), {"status": "rejected"})
                self.assertEqual(git_optional(repo, "rev-parse", "--verify", "--quiet", "refs/stash"), "")

    def test_group_writable_delivery_lock_is_rejected(self) -> None:
        """Trusting a group-writable lock file would let another account replace coordination state."""
        host = load_host()
        with tempfile.TemporaryDirectory() as raw:
            lock = Path(raw) / "harness-deliver.lock"
            lock.touch(); lock.chmod(0o666)
            with self.assertRaisesRegex(ValueError, "unsafe delivery lock"):
                host._lock(Path(raw))

    def test_live_origin_drift_is_rejected_before_confirmation(self) -> None:
        """Prompting before the live-origin check could approve a stale pinned remote."""
        host = load_host()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); repo, request = make_stash_request(root, host); writer = root / "writer"
            subprocess.run(["/usr/bin/git", "clone", str(root / "origin.git"), str(writer)], check=True, capture_output=True)
            git(writer, "config", "user.email", "test@example.invalid"); git(writer, "config", "user.name", "Test")
            (writer / "incoming.txt").write_text("remote\n"); git(writer, "add", "incoming.txt"); git(writer, "commit", "-m", "incoming"); git(writer, "push")
            prompted = []
            result = host.execute_request(request, confirm=lambda _prompt: prompted.append(True) or True)
            self.assertEqual(result, {"status": "rejected-drift"})
            self.assertEqual(prompted, [])
            self.assertEqual(git_optional(repo, "rev-parse", "--verify", "--quiet", "refs/stash"), "")

    def test_all_malformed_operation_shapes_are_rejected_without_git(self) -> None:
        """Membership on an unhashable operation must not bypass the fail-closed request boundary."""
        host = load_host()
        with tempfile.TemporaryDirectory() as raw:
            repo, request = make_stash_request(Path(raw), host)
            for value in ({}, [], 1, True, None):
                malformed = dict(request); malformed["operation"] = value
                self.assertEqual(host.execute_request(malformed, confirm=lambda _prompt: True), {"status": "rejected"})
                self.assertEqual(git_optional(repo, "rev-parse", "--verify", "--quiet", "refs/stash"), "")

    def test_evidence_mode_owner_and_group_drift_rejects_before_mutation(self) -> None:
        """Binding only evidence bytes/inode/time accepts a changed reviewed-file identity."""
        host = load_host()
        with tempfile.TemporaryDirectory() as raw:
            repo, request = make_stash_request(Path(raw), host)
            evidence = Path(raw).resolve() / "review.json"; evidence.write_text("reviewed\n"); evidence.chmod(0o600)
            request["evidence"] = [{"path": str(evidence), "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest()}]
            before = evidence.stat()
            def accept_then_chmod(_prompt: str) -> bool:
                os.chmod(evidence, 0o666); os.utime(evidence, ns=(before.st_atime_ns, before.st_mtime_ns)); return True
            self.assertEqual(host.execute_request(request, confirm=accept_then_chmod), {"status": "rejected-drift"})
            self.assertEqual(git_optional(repo, "rev-parse", "--verify", "--quiet", "refs/stash"), "")

    def test_prompt_displays_the_same_fixed_policy_argv_that_executes(self) -> None:
        """A dialog omitting fixed -c policy arguments cannot bind approval to the actual Git command."""
        host = load_host()
        with tempfile.TemporaryDirectory() as raw:
            repo, request = make_stash_request(Path(raw), host); prompts = []
            self.assertEqual(host.execute_request(request, confirm=lambda prompt: prompts.append(prompt) or True)["status"], "success")
            self.assertEqual(len(prompts), 1)
            self.assertIn('"-c","core.fsmonitor=false"', prompts[0])
            self.assertIn('"--literal-pathspecs","stash","push"', prompts[0])
            self.assertIn(f"origin={Path(raw).resolve() / 'origin.git'}", prompts[0])

    def test_fast_forward_moves_exactly_to_the_pinned_remote_head(self) -> None:
        """Merging an unpinned ref could advance main to a different commit than the approved target."""
        host = load_host()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); repo, request = make_stash_request(root, host); writer = root / "writer"
            git(repo, "restore", "tracked.txt")
            subprocess.run(["/usr/bin/git", "clone", str(root / "origin.git"), str(writer)], check=True, capture_output=True)
            git(writer, "config", "user.email", "test@example.invalid"); git(writer, "config", "user.name", "Test")
            (writer / "incoming.txt").write_text("remote\n"); git(writer, "add", "incoming.txt"); git(writer, "commit", "-m", "incoming"); git(writer, "push")
            git(repo, "fetch", "origin")
            snapshot = host.capture_snapshot(repo); target = git(repo, "rev-parse", "refs/remotes/origin/main")
            request.update({"operation": "ff-only", "snapshot_sha256": snapshot["sha256"], "expected_head": git(repo, "rev-parse", "HEAD"), "expected_remote": target, "target": target})
            request.pop("paths")
            result = host.execute_request(request, confirm=lambda _prompt: True)
            self.assertEqual(result, {"status": "success"})
            self.assertEqual(git(repo, "rev-parse", "HEAD"), target)
            self.assertEqual((repo / "incoming.txt").read_text(), "remote\n")

    def test_fast_forward_preserves_an_ignored_collision(self) -> None:
        """Removing --no-overwrite-ignore could overwrite an ignored local file during merge."""
        host = load_host()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve(); remote = root / "origin.git"; repo = root / "repo"; writer = root / "writer"
            subprocess.run(["/usr/bin/git", "init", "--bare", str(remote)], check=True, capture_output=True)
            repo.mkdir(); git(repo, "init", "-b", "main"); git(repo, "config", "user.email", "test@example.invalid"); git(repo, "config", "user.name", "Test")
            (repo / ".gitignore").write_text("collision.txt\n"); (repo / "tracked.txt").write_text("base\n")
            git(repo, "add", ".gitignore", "tracked.txt"); git(repo, "commit", "-m", "base"); git(repo, "remote", "add", "origin", str(remote)); git(repo, "push", "-u", "origin", "main")
            subprocess.run(["/usr/bin/git", "clone", str(remote), str(writer)], check=True, capture_output=True)
            git(writer, "config", "user.email", "test@example.invalid"); git(writer, "config", "user.name", "Test")
            (writer / "collision.txt").write_text("remote tracked\n"); git(writer, "add", "-f", "collision.txt"); git(writer, "commit", "-m", "incoming"); git(writer, "push")
            (repo / "collision.txt").write_text("local ignored\n"); git(repo, "fetch", "origin")
            snapshot = host.capture_snapshot(repo)
            request = {"version": 1, "operation": "ff-only", "repo": str(repo), "expected_head": git(repo, "rev-parse", "HEAD"), "expected_remote": git(repo, "rev-parse", "refs/remotes/origin/main"), "snapshot_sha256": snapshot["sha256"], "evidence": [], "expires_at": int(time.time()) + 60, "session": "test", "target": git(repo, "rev-parse", "refs/remotes/origin/main")}
            result = host.execute_request(request, confirm=lambda _prompt: True)
            self.assertEqual(result, {"status": "failed-or-partial"})
            self.assertEqual((repo / "collision.txt").read_text(), "local ignored\n")
            self.assertEqual(git(repo, "rev-parse", "HEAD"), request["expected_head"])

    def test_untracked_stash_path_is_rejected_before_confirmation(self) -> None:
        """Allowing an untracked requested path would broaden the displayed stash operation."""
        host = load_host()
        with tempfile.TemporaryDirectory() as raw:
            repo, request = make_stash_request(Path(raw), host)
            request["paths"] = ["not-tracked.txt"]
            prompted = []
            result = host.execute_request(request, confirm=lambda _prompt: prompted.append(True) or True)
            self.assertEqual(result, {"status": "rejected"})
            self.assertEqual(prompted, [])
            self.assertEqual(git_optional(repo, "rev-parse", "--verify", "--quiet", "refs/stash"), "")

    def test_missing_session_binding_is_rejected_before_confirmation(self) -> None:
        """Accepting a partial request would leave the fixed dialog without its required label."""
        host = load_host()
        with tempfile.TemporaryDirectory() as raw:
            repo, request = make_stash_request(Path(raw), host)
            request.pop("session")
            prompted = []
            result = host.execute_request(request, confirm=lambda _prompt: prompted.append(True) or True)
            self.assertEqual(result, {"status": "rejected"})
            self.assertEqual(prompted, [])
            self.assertEqual(git_optional(repo, "rev-parse", "--verify", "--quiet", "refs/stash"), "")

    def test_symlinked_repository_request_is_rejected_without_confirmation(self) -> None:
        """Resolving a repo symlink before validating it would authorize a redirected checkout."""
        host = load_host()
        with tempfile.TemporaryDirectory() as raw:
            raw_path = Path(raw)
            repo, request = make_stash_request(raw_path, host)
            linked = raw_path / "repo-link"
            linked.symlink_to(repo, target_is_directory=True)
            request["repo"] = str(linked)
            prompted = []
            result = host.execute_request(request, confirm=lambda _prompt: prompted.append(True) or True)
            self.assertEqual(result, {"status": "rejected"})
            self.assertEqual(prompted, [])
            self.assertEqual(git_optional(repo, "rev-parse", "--verify", "--quiet", "refs/stash"), "")

    def test_cli_rejects_duplicate_and_unknown_json_without_invoking_a_dialog(self) -> None:
        """Weak JSON loading could let duplicated or extra authority fields through."""
        cli = ROOT / "bin" / "trusted_preservation.py"
        for payload in (b'{"version":1,"version":1}', b'{"unknown":true}'):
            result = subprocess.run(["/usr/bin/python3", "-I", "-B", str(cli)], input=payload, capture_output=True)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout), {"status": "rejected"})

    def test_expired_request_never_reaches_confirmation_or_git(self) -> None:
        """Removing the deadline gate would prompt and mutate an expired request."""
        host = load_host()
        with tempfile.TemporaryDirectory() as raw:
            repo, request = make_stash_request(Path(raw), host)
            request["expires_at"] = int(time.time()) - 1
            prompted = []
            result = host.execute_request(request, confirm=lambda _prompt: prompted.append(True) or True)
            self.assertEqual(result["status"], "rejected")
            self.assertEqual(prompted, [])
            self.assertEqual(git_optional(repo, "rev-parse", "--verify", "--quiet", "refs/stash"), "")

    def test_exact_retained_stash_apply_restores_the_requested_worktree(self) -> None:
        """Changing apply to pop/drop or a symbolic stash must fail this restoration."""
        host = load_host()
        with tempfile.TemporaryDirectory() as raw:
            repo, request = make_stash_request(Path(raw), host)
            git(repo, "stash", "push", "-m", "fixture", "--", "tracked.txt")
            stash = git(repo, "rev-parse", "refs/stash")
            snapshot = host.capture_snapshot(repo)
            request.update({"operation": "apply-index", "snapshot_sha256": snapshot["sha256"], "expected_head": git(repo, "rev-parse", "HEAD"), "stash": stash})
            request.pop("paths")
            result = host.execute_request(request, confirm=lambda _prompt: True)
            self.assertEqual(result["status"], "success")
            self.assertEqual((repo / "tracked.txt").read_text(), "pending\n")
            self.assertEqual(git(repo, "rev-parse", "refs/stash"), stash)

    def test_fast_forward_from_non_main_branch_is_rejected_without_merge(self) -> None:
        """Dropping the main-branch gate must not let the host merge feature work."""
        host = load_host()
        with tempfile.TemporaryDirectory() as raw:
            repo, request = make_stash_request(Path(raw), host)
            snapshot = host.capture_snapshot(repo)
            git(repo, "restore", "tracked.txt")
            git(repo, "checkout", "-b", "feature")
            request.update({"operation": "ff-only", "snapshot_sha256": snapshot["sha256"], "target": request["expected_remote"]})
            request.pop("paths")
            result = host.execute_request(request, confirm=lambda _prompt: True)
            self.assertEqual(result["status"], "rejected")
            self.assertEqual(git(repo, "branch", "--show-current"), "feature")

    def test_accepted_unchanged_request_creates_a_retained_stash(self) -> None:
        """A read-only recheck must not manufacture drift and block the one action."""
        host = load_host()
        with tempfile.TemporaryDirectory() as raw:
            repo, request = make_stash_request(Path(raw), host)
            result = host.execute_request(request, confirm=lambda _prompt: True)
            self.assertEqual(result["status"], "success")
            self.assertTrue(git(repo, "rev-parse", "--verify", "refs/stash"))
            self.assertEqual(git(repo, "status", "--porcelain"), "")

    def test_drift_while_confirmation_waits_runs_no_git_mutation(self) -> None:
        """A post-dialog content change rejects before the requested stash."""
        host = load_host()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            repo = root / "repo"
            remote = root / "origin.git"
            subprocess.run(["/usr/bin/git", "init", "--bare", str(remote)], check=True, capture_output=True)
            repo.mkdir()
            git(repo, "init", "-b", "main")
            git(repo, "config", "user.email", "test@example.invalid")
            git(repo, "config", "user.name", "Test")
            (repo / "tracked.txt").write_text("base\n")
            git(repo, "add", "tracked.txt")
            git(repo, "commit", "-m", "base")
            git(repo, "remote", "add", "origin", str(remote))
            git(repo, "push", "-u", "origin", "main")
            (repo / "tracked.txt").write_text("pending\n")
            snapshot = host.capture_snapshot(repo)
            request = {
                "version": 1,
                "operation": "stash-paths",
                "repo": str(repo),
                "expected_head": git(repo, "rev-parse", "HEAD"),
                "expected_remote": git(repo, "rev-parse", "refs/remotes/origin/main"),
                "snapshot_sha256": snapshot["sha256"],
                "evidence": [],
                "expires_at": int(time.time()) + 60,
                "session": "test",
                "paths": ["tracked.txt"],
            }

            def accept_then_drift(_prompt: str) -> bool:
                (repo / "tracked.txt").write_text("changed during confirmation\n")
                return True

            result = host.execute_request(request, confirm=accept_then_drift)
            self.assertEqual(result["status"], "rejected-drift")
            self.assertEqual(git(repo, "status", "--porcelain"), "M tracked.txt")
            self.assertEqual(git_optional(repo, "rev-parse", "--verify", "--quiet", "refs/stash"), "")

    def test_evidence_drift_while_confirmation_waits_runs_no_git_mutation(self) -> None:
        """Removing evidence revalidation must not allow a stale reviewed backup."""
        host = load_host()
        with tempfile.TemporaryDirectory() as raw:
            repo, request = make_stash_request(Path(raw), host)
            evidence = Path(raw).resolve() / "backup.json"
            evidence.write_text("reviewed backup\n")
            request["evidence"] = [{"path": str(evidence), "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest()}]

            def accept_then_tamper(_prompt: str) -> bool:
                evidence.write_text("tampered\n")
                return True

            result = host.execute_request(request, confirm=accept_then_tamper)
            self.assertEqual(result["status"], "rejected-drift")
            self.assertEqual(git_optional(repo, "rev-parse", "--verify", "--quiet", "refs/stash"), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
