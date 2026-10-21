"""Managed policy remains enforced without granting sibling files or writes."""
import importlib.util
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'bin'))
import harness_headless as headless


class ManagedPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='.codex-policy-', dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.policy = self.root/'requirements.toml'
        self.policy.write_text('allow_remote_control = false\n')
        self.policy.chmod(0o644)
        self.sibling = self.root/'credential'
        self.sibling.write_text('secret-fixture')
        self.real_lstat = os.lstat

    def module(self):
        path = ROOT/'bin/harness_codex_policy.py'
        self.assertTrue(path.is_file(), 'trusted managed-policy read boundary is not implemented')
        spec = importlib.util.spec_from_file_location('policy_under_test', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.REQUIREMENTS = self.policy
        return module

    def root_owned(self, path, *args, **kwargs):
        # Root ownership is an external prerequisite unavailable to an ordinary
        # CI user. Keep real filesystem types, modes, links and missing entries.
        data = list(self.real_lstat(path, *args, **kwargs)); data[4] = 0
        return os.stat_result(data)

    def rule(self, module, metadata=None):
        with patch.object(module.os, 'lstat', side_effect=metadata or self.root_owned):
            return module.policy_read_rule()

    def sandbox(self, rule, *command):
        for name in ('candidate', 'scratch', 'home', 'source', 'state'):
            (self.root/name).mkdir(exist_ok=True)
        base = subprocess.check_output([str(ROOT/'bin/session-isolation.sh'),
            'verifier-sandbox-profile', str(self.root/'candidate'), str(self.root/'scratch'),
            str(self.root/'home'), str(self.root/'source'), '/usr/bin/true'],
            env={'HOME':str(self.root/'home'),'PATH':'/usr/bin:/bin',
                 'HARNESS_SESSION_STATE_HOME':str(self.root/'state')}, text=True)
        return subprocess.run(['/usr/bin/sandbox-exec', '-p', base+rule, *command],
                              env={'PATH':'/usr/bin:/bin'}, capture_output=True, text=True, timeout=10)

    def test_trusted_policy_is_readable_but_not_siblings_or_writes(self):
        rule = self.rule(self.module())
        result = self.sandbox(rule, '/bin/cat', str(self.policy))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'allow_remote_control = false\n')
        self.assertNotEqual(self.sandbox(rule, '/bin/cat', str(self.sibling)).returncode, 0)
        self.assertNotEqual(self.sandbox(rule, '/bin/sh', '-c', 'printf changed >> "$1"', 'sh', str(self.policy)).returncode, 0)
        self.assertEqual(self.policy.read_text(), 'allow_remote_control = false\n')

    def test_absent_policy_does_not_grant_parent_access(self):
        module = self.module(); self.policy.unlink()
        rule = self.rule(module)
        self.assertNotEqual(self.sandbox(rule, '/bin/cat', str(self.sibling)).returncode, 0)

    def test_symlink_file_is_refused(self):
        module = self.module(); self.policy.unlink(); self.policy.symlink_to(self.sibling)
        with self.assertRaises(ValueError): self.rule(module)

    def test_symlink_parent_is_refused(self):
        module = self.module(); redirect = self.root/'redirect'; redirect.symlink_to(self.root, target_is_directory=True)
        module.REQUIREMENTS = redirect/'requirements.toml'
        with self.assertRaises(ValueError): self.rule(module)

    def test_untrusted_owner_is_refused(self):
        module = self.module()
        def owned(path, *args, **kwargs):
            info = self.root_owned(path, *args, **kwargs)
            if Path(path) == self.policy:
                fields = list(info); fields[4] = 12345; return os.stat_result(fields)
            return info
        with self.assertRaises(ValueError): self.rule(module, owned)

    def test_writable_parent_or_file_is_refused(self):
        module = self.module()
        for target in (self.root, self.policy):
            original = stat.S_IMODE(target.stat().st_mode)
            target.chmod(original|0o020)
            try:
                with self.assertRaises(ValueError): self.rule(module)
            finally: target.chmod(original)

    def test_hardlink_and_special_file_are_refused(self):
        module = self.module(); os.link(self.policy, self.root/'second-link')
        with self.assertRaises(ValueError): self.rule(module)
        self.policy.unlink(); os.mkfifo(self.policy)
        with self.assertRaises(ValueError): self.rule(module)

    def test_metadata_error_is_not_treated_as_absence(self):
        module = self.module()
        def unavailable(path, *args, **kwargs):
            if Path(path) == self.policy: raise PermissionError('policy metadata unavailable')
            return self.root_owned(path, *args, **kwargs)
        with self.assertRaises(ValueError): self.rule(module, unavailable)

    def test_profile_helper_is_found_through_relative_entrypoint_symlink(self):
        entry = self.root/'entry'; entry.mkdir()
        link = entry/'harness-session'
        link.symlink_to(os.path.relpath(ROOT/'bin/session-isolation.sh', entry))
        for name in ('candidate', 'scratch', 'home', 'source', 'state'):
            (self.root/name).mkdir()
        result = subprocess.run([str(link), 'codex-sandbox-profile',
            str(self.root/'candidate'), str(self.root/'scratch'), str(self.root/'home'),
            str(self.root/'source'), '/usr/bin/true', '54321', str(self.root/'home'), '/usr/bin/true'],
            env={'HOME':str(self.root/'home'), 'PATH':'/usr/bin:/bin',
                 'HARNESS_SESSION_STATE_HOME':str(self.root/'state')},
            capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_configuration_bootstrap_failure_is_refused_during_preflight(self):
        # A version-only probe lets an unusable CLI through to a model run.
        bin_dir = self.root/'bin'; bin_dir.mkdir()
        exe = bin_dir/'codex'; host = bin_dir/'codex-code-mode-host'
        exe.write_text('#!/bin/sh\ncase "$*" in\n--version) echo codex-test; exit 0;;\n"features list") echo test-policy-load-failed >&2; exit 70;;\n*) exit 64;;\nesac\n')
        host.write_text('#!/bin/sh\nexit 0\n')
        exe.chmod(0o755); host.chmod(0o755)
        profile = self.root/'preflight.sb'
        profile.write_text('(version 1)\n(allow default)\n(deny file-read* (literal '+__import__('json').dumps(str(self.sibling))+'))\n')
        with self.assertRaisesRegex(headless.Refused, 'test-policy-load-failed'):
            headless.codex_preflight(str(profile), str(exe), str(host), str(self.sibling),
                                     {'HOME':str(self.root), 'PATH':'/usr/bin:/bin'}, str(self.root))


if __name__ == '__main__': unittest.main()
