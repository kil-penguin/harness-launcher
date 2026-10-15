from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["/usr/bin/git", "-C", str(repo), *args], check=True,
        text=True, capture_output=True, env={"PATH": "/usr/bin:/bin", "GIT_OPTIONAL_LOCKS": "0"}).stdout.strip()


def load():
    spec = importlib.util.spec_from_file_location("preservation_snapshot", ROOT / "bin/preservation_snapshot.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


class SnapshotTest(unittest.TestCase):
    def test_snapshot_rejects_assume_unchanged_index_entries(self):
        """Ignoring lowercase ls-files flags would omit a deliberately hidden dirty file."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve(); remote = root / "remote.git"; repo = root / "repo"
            subprocess.run(["/usr/bin/git", "init", "--bare", str(remote)], check=True, capture_output=True)
            repo.mkdir(); git(repo, "init", "-b", "main"); git(repo, "config", "user.email", "a@b.invalid"); git(repo, "config", "user.name", "a")
            (repo / "a.txt").write_text("base\n"); git(repo, "add", "a.txt"); git(repo, "commit", "-m", "base"); git(repo, "remote", "add", "origin", str(remote)); git(repo, "push", "-u", "origin", "main")
            git(repo, "update-index", "--assume-unchanged", "a.txt")
            with self.assertRaisesRegex(ValueError, "hidden index flags"):
                load().capture_snapshot(repo)

    def test_snapshot_captures_pending_node_and_index_without_mutation(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve(); remote = root / "remote.git"; repo = root / "repo"
            subprocess.run(["/usr/bin/git", "init", "--bare", str(remote)], check=True, capture_output=True)
            repo.mkdir(); git(repo, "init", "-b", "main"); git(repo, "config", "user.email", "a@b.invalid"); git(repo, "config", "user.name", "a")
            (repo / "a.txt").write_text("base\n"); git(repo, "add", "a.txt"); git(repo, "commit", "-m", "base"); git(repo, "remote", "add", "origin", str(remote)); git(repo, "push", "-u", "origin", "main")
            (repo / "a.txt").write_text("pending\n"); index = repo / ".git/index"; before = index.stat().st_mtime_ns
            snapshot = load().capture_snapshot(repo)
            self.assertEqual({"head", "remote", "status", "nodes", "index", "index_bytes", "stage", "flags", "refs", "common", "sha256", "index_nodes", "parents", "repoidentity"}, set(snapshot))
            self.assertEqual("file", snapshot["nodes"]["a.txt"]["type"])
            self.assertEqual(before, index.stat().st_mtime_ns)

    def test_snapshot_records_acl_xattrs_and_flags_for_pending_file(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "item"; path.write_text("x")
            subprocess.run(["/usr/bin/xattr", "-w", "user.snapshot", "value", str(path)], check=True)
            metadata = load()._meta(path)
            self.assertIn("acl", metadata)
            self.assertIn("xattrs_sha256", metadata)
            self.assertIn("user.snapshot", metadata["xattrs_sha256"])
            self.assertIn("flags", metadata)


if __name__ == "__main__":
    unittest.main()
