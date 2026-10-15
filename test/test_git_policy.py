from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load():
    spec = importlib.util.spec_from_file_location("git_policy", ROOT / "bin/git_policy.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def git(repo: Path, *args: str) -> None:
    subprocess.run(["/usr/bin/git", "-C", str(repo), *args], check=True, capture_output=True)


class GitPolicyTest(unittest.TestCase):
    def test_rejects_executable_relevant_hook(self):
        """An executable post-merge hook could run an unreviewed program during the exact operation."""
        policy = load()
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw).resolve() / "repo"; repo.mkdir(); git(repo, "init", "-b", "main")
            hook = repo / ".git/hooks/post-merge"; hook.write_text("#!/bin/sh\n"); hook.chmod(0o755)
            with self.assertRaisesRegex(ValueError, "hook"):
                policy._hooks(repo)

    def test_rejects_executable_hook_at_effective_core_hooks_path(self):
        """Ignoring core.hooksPath lets a configured reference hook run during preservation."""
        policy = load()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve(); repo = root / "repo"; hooks = root / "reviewed-hooks"
            repo.mkdir(); hooks.mkdir(); git(repo, "init", "-b", "main"); origin = root / "origin.git"
            subprocess.run(["/usr/bin/git", "init", "--bare", str(origin)], check=True, capture_output=True); git(repo, "remote", "add", "origin", str(origin))
            git(repo, "config", "core.hooksPath", str(hooks))
            hook = hooks / "reference-transaction"; hook.write_text("#!/bin/sh\n"); hook.chmod(0o755)
            with self.assertRaisesRegex(ValueError, "hook"):
                policy.validate(repo)

    def test_rejects_custom_merge_driver_configuration(self):
        """A merge driver can execute while applying the approved retained stash."""
        policy = load()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve(); repo = root / "repo"; repo.mkdir(); git(repo, "init", "-b", "main"); origin = root / "origin.git"
            subprocess.run(["/usr/bin/git", "init", "--bare", str(origin)], check=True, capture_output=True); git(repo, "remote", "add", "origin", str(origin))
            git(repo, "config", "merge.hostile.driver", "/usr/bin/touch should-not-run")
            with self.assertRaisesRegex(ValueError, "unsafe git config"):
                policy.validate(repo)

    def test_rejects_ext_origin_helper(self):
        """An ext:: origin can execute a caller-selected helper during live remote lookup."""
        policy = load()
        with self.assertRaisesRegex(ValueError, "origin"):
            policy._origin(("remote.origin.url\next::touch /tmp/pwn",))

    def test_rejects_nonbare_local_origin_even_with_a_head_file(self):
        """A checkout directory is not the reviewed local bare transport, despite its HEAD file."""
        policy = load()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve(); repo = root / "repo"; origin = root / "origin"
            repo.mkdir(); origin.mkdir(); git(repo, "init", "-b", "main"); git(origin, "init", "-b", "main")
            git(repo, "remote", "add", "origin", str(origin))
            with self.assertRaisesRegex(ValueError, "origin"):
                policy.validate(repo)

    def test_fixed_ssh_command_disables_user_config_and_interactive_callbacks(self):
        """A caller SSH config could supply ProxyCommand or LocalCommand during remote lookup."""
        policy = load()
        argv = policy.argv(Path("/tmp/repo"), "status")
        self.assertIn("core.sshCommand=/usr/bin/ssh -F /dev/null -oBatchMode=yes -oPermitLocalCommand=no", argv)

    def test_bound_ssh_remote_query_uses_only_the_explicit_github_identity(self):
        """Letting a configured agent or alias choose authentication would escape the reviewed binding."""
        policy = load()
        identity = "/private/tmp/trusted-key"
        argv = policy.remote_argv(
            Path("/tmp/repo"), "git@reviewed-alias:owner/repository.git",
            {"hostname": "github.com", "user": "git", "identity_file": identity},
        )
        command = next(value for value in argv if value.startswith("core.sshCommand="))
        self.assertEqual(
            command,
            "core.sshCommand=/usr/bin/ssh -F /dev/null -oBatchMode=yes -oPermitLocalCommand=no "
            "-oIdentitiesOnly=yes -oIdentityAgent=none -oCertificateFile=none -oHostName=github.com -oUser=git -i /private/tmp/trusted-key",
        )
        self.assertEqual(argv[-2:], ["git@reviewed-alias:owner/repository.git", "refs/heads/main"])

    def test_bound_ssh_rejects_scp_paths_with_dot_components(self):
        """A dot owner or repository component makes the displayed SCP path ambiguous."""
        policy = load()
        binding = {"hostname": "github.com", "user": "git", "identity_file": "/private/tmp/trusted-key"}
        for url in ("git@alias:./repository.git", "git@alias:owner/...git", "https://github.com/owner/repository.git", "/private/tmp/repository.git", "ssh://git@github.com/owner/repository.git"):
            with self.assertRaisesRegex(ValueError, "origin"):
                policy.remote_argv(Path("/tmp/repo"), url, binding)


if __name__ == "__main__":
    unittest.main()
