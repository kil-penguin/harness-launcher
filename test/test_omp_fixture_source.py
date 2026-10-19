"""Behavioral regression tests for the portable source fixture and real consumers.

Removing admission, selecting ambient input, resealing payload hashes, or delaying
admission until after a child/import must break these tests. Native drivers and
assertions in the two existing suites remain unchanged.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / 'test/fixtures/w07-source'


class FixtureCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='omp-fixture-admission-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.checkout = self.base / 'checkout with spaces'
        self.test = self.checkout / 'test'
        self.test.mkdir(parents=True, mode=0o700)
        self.root = self.test / 'fixtures/w07-source'
        self.root.parent.mkdir(mode=0o700)
        shutil.copytree(FIXTURE, self.root)
        for name in ['omp-fixture-source.py', 'omp-fixture-policy.json']:
            shutil.copy2(REPO / 'test' / name, self.test / name)
        # Historical private baseline needs its original adjacent policy for RED;
        # the portable candidate has no dependency on this optional file.
        old = REPO / 'test/admission-policy.json'
        if old.exists():
            shutil.copy2(old, self.test / old.name)
        self.home = self.base / 'home'
        self.home.mkdir(mode=0o700)
        self.tmp = self.base / 'tmp'
        self.tmp.mkdir(mode=0o700)
        self.env = {'HOME': str(self.home), 'TMPDIR': str(self.tmp),
                    'PATH': '/usr/bin:/bin:/opt/homebrew/bin', 'LC_ALL': 'C',
                    'TERM': 'dumb', 'PYTHONDONTWRITEBYTECODE': '1'}

    def capture(self, label, argv, env, timeout=5):
        start = time.monotonic()
        result = subprocess.run(argv, cwd=REPO, env=env, capture_output=True,
                                text=True, timeout=timeout)
        print(json.dumps({'child': label, 'argv': argv, 'exit': result.returncode,
                          'elapsed_seconds': time.monotonic() - start,
                          'environment_keynames': sorted(env),
                          'stdout': result.stdout, 'stderr': result.stderr}), flush=True)
        return result

    def helper(self, selected=None):
        env = dict(self.env)
        if selected is not None:
            env['HARNESS_TEST_OMP_SOURCE'] = str(selected)
        return self.capture('admission', [sys.executable, '-I', '-S', '-B',
                                         str(self.test / 'omp-fixture-source.py')], env)

    def refused(self, selected=None):
        result = self.helper(self.root if selected is None else selected)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertFalse((self.base / 'payload-imported').exists())

    def rewrite_manifest(self, change):
        p = self.root / 'manifest.json'
        value = json.loads(p.read_text())
        change(value)
        p.write_text(json.dumps(value))


class FixtureAdmission(FixtureCase):
    def test_default_uses_current_owned_relocated_checkout(self):
        self.assertEqual(self.checkout.stat().st_uid, os.getuid())
        self.assertEqual((self.root / 'core/scripts/harness_compile.py').stat().st_uid,
                         os.getuid())
        result = self.helper()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, str(self.root) + '\n')

    def test_explicit_equal_paired_source_is_admitted(self):
        paired = self.base / 'paired with spaces'
        shutil.copytree(self.root, paired)
        result = self.helper(paired)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, str(paired) + '\n')

    def test_missing_root_refused(self):
        self.refused(self.base / 'missing')

    def test_empty_explicit_does_not_fall_back(self):
        self.refused('')

    def test_relative_root_refused(self):
        self.refused('test/fixtures/w07-source')

    def test_dotdot_root_refused(self):
        self.refused(str(self.root) + '/../w07-source')

    def test_missing_synthetic_input_refused(self):
        (self.root / '.claude/source/budgets.yaml').unlink()
        self.refused()

    def test_missing_manifest_refused(self):
        (self.root / 'manifest.json').unlink()
        self.refused()

    def test_tampered_code_refused_before_import(self):
        (self.root / 'core/scripts/harness_compile.py').write_text(
            'from pathlib import Path\nPath(' + repr(str(self.base / 'payload-imported')) + ').touch()\n')
        self.refused()

    def test_resealed_manifest_cannot_replace_trusted_source(self):
        p = self.root / 'core/scripts/harness_compile.py'
        p.write_bytes(p.read_bytes() + b'\n# synthetic tamper\n')
        def change(value):
            row = next(x for x in value['files'] if x['path'] == 'core/scripts/harness_compile.py')
            row.update(sha256=hashlib.sha256(p.read_bytes()).hexdigest(), bytes=p.stat().st_size)
        self.rewrite_manifest(change)
        self.refused()

    def test_tampered_adjacent_policy_refused(self):
        p = self.test / 'omp-fixture-policy.json'
        p.write_bytes(p.read_bytes() + b'\n')
        self.refused()

    def test_wrong_revision_refused(self):
        self.rewrite_manifest(lambda value: value.update(revision='0' * 40))
        self.refused()

    def test_owner_declaration_cannot_override_actual_owner_check(self):
        # Only synthetic metadata; no actual foreign-owned filesystem claim.
        self.rewrite_manifest(lambda value: value.update(owner_uid=os.getuid() + 1))
        self.refused()

    def test_manifest_path_escape_refused(self):
        self.rewrite_manifest(lambda value: value['files'][0].update(path='../escape'))
        self.refused()

    def test_root_symlink_refused(self):
        link = self.base / 'root-link'
        link.symlink_to(self.root, target_is_directory=True)
        self.refused(link)

    def test_parent_symlink_refused(self):
        link = self.base / 'parent-link'
        link.symlink_to(self.root.parent, target_is_directory=True)
        self.refused(link / self.root.name)

    def test_file_symlink_refused(self):
        p = self.root / '.claude/source/budgets.yaml'
        p.unlink()
        p.symlink_to(FIXTURE / '.claude/source/budgets.yaml')
        self.refused()

    def test_hardlink_refused(self):
        os.link(self.root / '.claude/source/budgets.yaml', self.base / 'hardlink')
        self.refused()

    def test_writable_file_refused(self):
        (self.root / '.claude/source/budgets.yaml').chmod(0o666)
        self.refused()

    def test_writable_directory_refused(self):
        self.root.chmod(0o777)
        self.refused()

    def test_extra_payload_refused(self):
        (self.root / 'unselected.py').write_text('raise RuntimeError("never import")\n')
        self.refused()


class Consumers(FixtureCase):
    def suite(self, name, count, explicit):
        env = dict(self.env)
        if explicit:
            env['HARNESS_TEST_OMP_SOURCE'] = str(FIXTURE)
        result = self.capture(name + ('-explicit' if explicit else '-default'),
                              ['bash', str(REPO / 'test' / name)], env, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Ran ' + str(count) + ' tests', result.stderr)
        self.assertTrue(result.stderr.rstrip().endswith('OK'), result.stderr)
        self.assertNotIn('skipped=', result.stderr)
        if count == 26:
            for oracle in ['test_C01_C03_C27_public_alias_layer_fake_only',
                           'test_C10_namespace_busy_no_native_or_receipt_transition',
                           'test_C11_workspace_busy_precedes_namespace_and_native',
                           'test_C21_foreground_native_nonzero_preserved']:
                self.assertIn(oracle, result.stderr)

    def test_direct_continuation_default_runs_all_26(self):
        self.suite('test-omp-continuation.sh', 26, False)

    def test_direct_required_default_runs_all_4(self):
        self.suite('test-omp-required-loading.sh', 4, False)

    def test_direct_continuation_explicit_runs_all_26(self):
        self.suite('test-omp-continuation.sh', 26, True)

    def test_direct_required_explicit_runs_all_4(self):
        self.suite('test-omp-required-loading.sh', 4, True)

    def test_all_three_consumers_refuse_before_child_or_payload(self):
        # Source selected by the old direct suites would write a marker on import.
        p = self.root / 'core/scripts/harness_compile.py'
        p.write_text('from pathlib import Path\nPath(' + repr(str(self.base / 'payload-imported')) + ').touch()\n')
        marker = self.base / 'runner-child'
        deny = self.base / 'deny-interpreter'
        deny.write_text('#!/bin/sh\nprintf child > "$HOME/../runner-child"\nexit 97\n')
        deny.chmod(0o700)
        env = dict(self.env, HARNESS_TEST_OMP_SOURCE=str(self.root),
                   BASH_BIN=str(deny), ZSH_BIN=str(deny))
        for name in ['run-all.sh', 'test-omp-continuation.sh', 'test-omp-required-loading.sh']:
            with self.subTest(consumer=name):
                result = self.capture(name + '-refuse', ['bash', str(REPO / 'test' / name)],
                                      env, timeout=60)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertFalse(marker.exists())
                self.assertFalse((self.base / 'payload-imported').exists())

    def runner_failure_metadata(self, shell, status, first_success=False):
        # Controlled shell test input, run by actual system interpreters. This
        # normal source-admitted runner stops before any real suite/full/Git.
        shutil.copy2(REPO / 'test/run-all.sh', self.test / 'run-all.sh')
        first = self.test / 'test-a-controlled.sh'
        first.write_text('#!/bin/' + shell + '\nprintf "controlled stdout\\n"\nprintf "controlled stderr\\n" >&2\nexit ' + str(0 if first_success else status) + '\n')
        first.chmod(0o644)
        second = self.test / 'test-z-controlled.sh'
        second.write_text('#!/bin/' + shell + '\nprintf second > "$HOME/second-reached"\nexit ' + str(status) + '\n')
        second.chmod(0o644)
        env = dict(self.env, BASH_BIN='/bin/bash', ZSH_BIN='/bin/zsh')
        result = self.capture('runner-controlled-' + shell, ['/bin/bash', str(self.test / 'run-all.sh')], env)
        self.assertEqual(result.returncode, status, result.stdout + result.stderr)
        self.assertIn('controlled stdout\n', result.stdout)
        failed = 'test-z-controlled.sh' if first_success else 'test-a-controlled.sh'
        expected = 'REGRESSION_CHILD_FAILED suite=' + failed + ' interpreter=' + shell + ' exit=' + str(status) + '\n'
        self.assertEqual(result.stderr, 'controlled stderr\n' + expected)
        self.assertEqual((self.home / 'second-reached').exists(), first_success)
        self.assertNotIn(str(self.checkout), result.stderr)
        self.assertNotIn(str(self.home), result.stderr)

    def test_runner_bash_failure_keeps_status_streams_and_failfast(self):
        self.runner_failure_metadata('bash', 23)

    def test_runner_zsh_failure_keeps_status_streams_and_failfast(self):
        self.runner_failure_metadata('zsh', 29)

    def test_runner_success_continues_and_reports_only_later_failure(self):
        self.runner_failure_metadata('bash', 31, first_success=True)

    def runner_binding(self, explicit):
        # Supported interpreter injection observes the actual first dispatch, then
        # deliberately exits97. This is NOT a full-runner or suite-PASS claim.
        probe = self.base / 'probe-interpreter'
        probe.write_text('#!/bin/sh\nprintf "%s" "${HARNESS_TEST_OMP_SOURCE-UNSET}" > "$HOME/../runner-binding"\nexit 97\n')
        probe.chmod(0o700)
        env = dict(self.env, BASH_BIN=str(probe), ZSH_BIN=str(probe))
        expected = FIXTURE
        if explicit:
            expected = self.root
            env['HARNESS_TEST_OMP_SOURCE'] = str(expected)
        result = self.capture('run-all-binding', ['bash', str(REPO / 'test/run-all.sh')],
                              env, timeout=5)
        self.assertEqual(result.returncode, 97, result.stdout + result.stderr)
        self.assertEqual((self.base / 'runner-binding').read_text(), str(expected))

    def test_runner_default_exports_admitted_root_to_first_dispatch(self):
        self.runner_binding(False)

    def test_runner_explicit_exports_admitted_root_to_first_dispatch(self):
        self.runner_binding(True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
