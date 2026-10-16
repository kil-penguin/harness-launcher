#!/usr/bin/env python3
"""Read-only report contracts: no GC/hold cleanup, unknown preserves, bounded IO."""
import datetime as dt
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
ID = '11111111-1111-4111-8111-111111111111'
OTHER = '22222222-2222-4222-8222-222222222222'
REV = 'a' * 40
NOW = dt.datetime(2026, 1, 3, tzinfo=dt.timezone.utc)


class SessionReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.state = self.base / 'state'
        self.runtime = self.base / 'runtimes'
        self.source = self.base / 'source'
        self.source.mkdir()
        self.root = self.state / 'worktrees' / ID
        self.root.mkdir(parents=True)
        self.record = self.state / 'sessions' / ID
        self.record.mkdir(parents=True)
        for name, value in {'source-root': str(self.source), 'session-root': str(self.root),
                            'base-sha': 'b' * 40, 'lease-v1': '1', 'runtime.lock': ''}.items():
            (self.record / name).write_text(value + ('\n' if value else ''))
        info = self.root.stat()
        (self.record / 'root-inode').write_text(f'{info.st_dev} {info.st_ino}\n')
        self.journal()
        (self.runtime / 'bindings').mkdir(parents=True)
        (self.runtime / REV).mkdir()
        pin = {'version': '1.2.3', 'revision': REV, 'path': str(self.runtime / REV), 'manifest_sha256': 'c' * 64}
        (self.runtime / 'current.json').write_text(json.dumps(pin))
        (self.runtime / 'bindings' / f'{ID}.json').write_text(json.dumps({'source_root': str(self.source), 'runtime': pin}))

    def journal(self, state='CLOSED', heartbeat='2026-01-01T00:00:00Z', identity=''):
        (self.record / 'journal').write_text(f'state={state}\nidentity={identity}\nheartbeat={heartbeat}\n')

    def module(self):
        path = ROOT / 'bin/harness_session_report.py'
        self.assertTrue(path.is_file(), 'read-only report implementation is missing')
        spec = importlib.util.spec_from_file_location('session_report_test_target', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def report(self, **kwargs):
        return self.module().build_report(state=self.state, runtime_home=self.runtime, now=NOW,
                                         current_session=OTHER, **kwargs)

    def item(self, **kwargs):
        return self.report(**kwargs)['sessions'][0]

    def cli(self, *args, state=None, retention='86400', entry=None, extra_env=None):
        env = {**os.environ, 'HOME': str(self.base), 'HARNESS_SESSION_STATE_HOME': str(state or self.state),
               'HARNESS_SESSION_RETENTION_SECONDS': retention, 'HARNESS_SESSION_ID': OTHER}
        env.update(extra_env or {})
        command = [str(entry)] if entry else ['/bin/bash', str(ROOT / 'bin/session-isolation.sh')]
        return subprocess.run([*command, 'report', '--json',
                               '--runtime-home', str(self.runtime), *args], env=env, capture_output=True, text=True, timeout=10)

    def snapshot(self, base=None):
        base = base or self.base
        result = {}
        for path in sorted(base.rglob('*')):
            info = path.lstat()
            result[str(path.relative_to(base))] = (info.st_mode, info.st_ino, info.st_nlink, info.st_size,
                info.st_mtime_ns, info.st_ctime_ns, os.readlink(path) if path.is_symlink() else (path.read_bytes() if path.is_file() else None))
        return result

    def installed_package(self):
        prefix = self.base / 'Homebrew with spaces'
        package = prefix / 'Cellar/harness-launcher/1.0/share/harness-launcher'
        package.mkdir(parents=True)
        for name in ('session-isolation.sh', 'harness-common.sh', 'harness_session_report.py'):
            shutil.copy2(ROOT / 'bin' / name, package / name)
        (prefix / 'bin').mkdir()
        return prefix, package

    def test_installed_absolute_symlink_reports_json_without_changing_fixture(self):
        prefix, package = self.installed_package()
        entry = prefix / 'bin/harness-session'
        entry.symlink_to(package / 'session-isolation.sh')
        before = self.snapshot()
        result = self.cli(entry=entry)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['sessions'][0]['session_id'], ID)
        self.assertEqual(before, self.snapshot())

    def test_installed_relative_symlink_chain_reports_json_without_changing_fixture(self):
        prefix, package = self.installed_package()
        (prefix / 'opt').mkdir()
        (prefix / 'opt/harness-launcher').symlink_to('../Cellar/harness-launcher/1.0', target_is_directory=True)
        middle = prefix / 'bin/session-link'
        middle.symlink_to('../opt/harness-launcher/share/harness-launcher/session-isolation.sh')
        entry = prefix / 'bin/harness-session'
        entry.symlink_to('session-link')
        before = self.snapshot()
        result = self.cli(entry=entry)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['sessions'][0]['session_id'], ID)
        self.assertEqual(before, self.snapshot())

    def test_installed_direct_path_keeps_python_imports_isolated(self):
        _, package = self.installed_package()
        poison = self.base / 'poison'
        poison.mkdir()
        for directory in (poison, package):
            (directory / 'json.py').write_text('raise RuntimeError("UNISOLATED_REPORT_IMPORT")\n')
        before = self.snapshot()
        result = self.cli(entry=package / 'session-isolation.sh', extra_env={
            'HARNESS_PYTHON_BIN': sys.executable, 'PYTHONPATH': str(poison)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['sessions'][0]['session_id'], ID)
        self.assertEqual(before, self.snapshot())

    def test_installed_missing_modules_fail_without_changing_fixture(self):
        _, package = self.installed_package()
        for name in ('harness-common.sh', 'harness_session_report.py'):
            with self.subTest(module=name):
                module = package / name
                content = module.read_bytes()
                module.unlink()
                try:
                    before = self.snapshot()
                    result = self.cli(entry=package / 'session-isolation.sh')
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(result.stdout, '')
                    self.assertIn('report modules unavailable', result.stderr)
                    self.assertEqual(before, self.snapshot())
                finally:
                    module.write_bytes(content)

    def test_installed_entry_becoming_symlink_cycle_fails_within_timeout(self):
        prefix, package = self.installed_package()
        entry = prefix / 'bin/harness-session'
        entry.symlink_to(package / 'session-isolation.sh')
        tools = self.base / 'tools'
        tools.mkdir()
        # The shell has already opened the script when resolution begins.
        # Inject a real filesystem cycle at that boundary, as a concurrent
        # install could do, so the resolver itself must stop following links.
        readlink = tools / 'readlink'
        readlink.write_text('#!/bin/sh\n/bin/ln -sf harness-session "$1"\nexec /usr/bin/readlink "$1"\n')
        readlink.chmod(0o755)
        before = self.snapshot(self.state), self.snapshot(self.runtime)
        result = self.cli(entry=entry, extra_env={'PATH': str(tools) + os.pathsep + os.environ['PATH']})
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertIn('cannot resolve report entry', result.stderr)
        self.assertEqual(os.readlink(entry), 'harness-session')
        self.assertEqual(before, (self.snapshot(self.state), self.snapshot(self.runtime)))

    def test_cli_is_read_only_even_with_stale_hold_and_sensitive_bodies(self):
        hold = self.record / 'resume-hold-stale.json'
        hold.write_text(json.dumps({'pid': 99999999, 'start': 'old process', 'source_root': str(self.source),
                                   'session_id': ID, 'request_id': 'stale'}))
        for relative in ('.harness/codex/auth.json', '.harness/codex/config.toml',
                         '.harness/codex/sessions/rollout.jsonl', '.claude/transcript.jsonl'):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('PRIVATE_BODY_SENTINEL')
        before = self.snapshot()
        result = self.cli('--sizes')
        self.assertEqual(result.returncode, 0, result.stderr)
        item = json.loads(result.stdout)['sessions'][0]
        self.assertIn('resume_hold_present', item['retained_reasons'])
        self.assertEqual(item['holds']['status'], 'present_unverified')
        self.assertEqual(before, self.snapshot())
        self.assertNotIn('PRIVATE_BODY_SENTINEL', result.stdout + result.stderr)

    def test_old_terminal_record_requires_native_verification_and_never_claims_delete_safe(self):
        item = self.item()
        self.assertEqual(item['lifecycle'], 'CLOSED')
        self.assertEqual(item['age_seconds'], 172800)
        self.assertEqual(item['retention_seconds'], 86400)
        self.assertEqual(item['eligibility'], 'needs_provenance_recovery')
        self.assertFalse(item['delete_safe'])
        self.assertIn('needs_native_verify', item['retained_reasons'])
        self.assertEqual(item['archive']['verification'], 'unknown')
        self.assertIsNone(item['sizes'])
        self.assertIsNone(self.report()['last_gc_trigger'])

    def test_all_concurrent_reasons_are_reported(self):
        self.journal(state='OPEN', heartbeat='2026-01-02T23:00:00Z')
        (self.record / 'resume-hold-bad.json').write_text('{}')
        item = self.module().build_report(state=self.state, runtime_home=self.runtime, now=NOW, current_session=ID)['sessions'][0]
        for reason in ('current_session', 'nonterminal_state', 'within_retention', 'resume_hold_present', 'resume_hold_unknown'):
            self.assertIn(reason, item['retained_reasons'])
        self.assertEqual(item['eligibility'], 'retained')

    def test_active_kernel_lease_protects_even_closed_old_session(self):
        with (self.record / 'runtime.lock').open('r+') as stream:
            fcntl.lockf(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        item = json.loads(result.stdout)['sessions'][0]
        self.assertEqual(item['lease'], 'active')
        self.assertIn('active_lease', item['retained_reasons'])
        self.assertEqual(item['eligibility'], 'retained')

    def test_age_and_lifecycle_do_not_make_abandoned_or_conflict_gc_terminal(self):
        for lifecycle in ('ABANDONED', 'CONFLICT', 'SUBMITTED', 'INTEGRATING'):
            with self.subTest(lifecycle=lifecycle):
                self.journal(state=lifecycle)
                self.assertIn('nonterminal_state', self.item()['retained_reasons'])

    def test_future_and_malformed_journals_preserve_unknown(self):
        self.journal(heartbeat='2026-01-04T00:00:00Z')
        self.assertIn('future_timestamp', self.item()['retained_reasons'])
        (self.record / 'journal').write_text('state=CLOSED\nstate=OPEN\n')
        item = self.item()
        self.assertIsNone(item['age_seconds'])
        self.assertEqual(item['lifecycle'], 'unknown')
        self.assertIn('journal_unknown', item['retained_reasons'])

    def test_replaced_root_inode_mismatched_path_and_symlink_are_not_scanned(self):
        (self.record / 'root-inode').write_text('1 2\n')
        item = self.item(sizes=True)
        self.assertIn('root_identity_mismatch', item['retained_reasons'])
        self.assertIsNone(item['sizes'])
        (self.record / 'session-root').write_text(str(self.source) + '\n')
        self.assertIn('root_path_mismatch', self.item()['retained_reasons'])
        (self.record / 'session-root').write_text(str(self.root) + '\n')
        self.root.rmdir()
        self.root.symlink_to(self.source, target_is_directory=True)
        self.assertIn('root_unknown', self.item()['retained_reasons'])

    def test_metadata_symlinks_unreadable_missing_and_oversized_are_unknown(self):
        module = self.module()
        original = module.os.open
        def denied(path, *args, **kwargs):
            if os.fspath(path) == 'source-root':
                raise PermissionError('synthetic inaccessible metadata')
            return original(path, *args, **kwargs)
        with patch.object(module.os, 'open', side_effect=denied):
            item = module.build_report(state=self.state, runtime_home=self.runtime, now=NOW)['sessions'][0]
        self.assertIsNone(item['source_root'])
        self.assertIn('source_unknown', item['retained_reasons'])
        (self.record / 'source-root').unlink()
        (self.record / 'source-root').symlink_to(self.base / 'missing')
        self.assertIn('source_unknown', self.item()['retained_reasons'])
        (self.record / 'source-root').unlink()
        (self.record / 'source-root').write_text('x' * 65537)
        self.assertIn('source_unknown', self.item()['retained_reasons'])

    def test_mismatched_runtime_binding_and_source_are_provenance_recovery(self):
        binding = self.runtime / 'bindings' / f'{ID}.json'
        data = json.loads(binding.read_text())
        data['source_root'] = str(self.base / 'other-source')
        binding.write_text(json.dumps(data))
        item = self.item()
        self.assertIn('runtime_source_mismatch', item['retained_reasons'])
        self.assertEqual(item['eligibility'], 'needs_provenance_recovery')
        data['source_root'] = str(self.source)
        data['runtime']['path'] = str(self.source)
        binding.write_text(json.dumps(data))
        self.assertIn('bound_runtime_unknown', self.item()['retained_reasons'])
        binding.unlink()
        self.assertEqual(self.item()['eligibility'], 'needs_provenance_recovery')

    def test_selected_runtime_difference_is_observation_not_reason_to_delete(self):
        pin = json.loads((self.runtime / 'current.json').read_text())
        pin.update(revision='d' * 40, path=str(self.runtime / ('d' * 40)))
        (self.runtime / ('d' * 40)).mkdir()
        (self.runtime / 'current.json').write_text(json.dumps(pin))
        item = self.item()
        self.assertTrue(item['runtime']['differs_from_selected'])
        self.assertEqual(item['runtime']['bound']['revision'], REV)
        self.assertEqual(item['runtime']['bound']['verification'], 'metadata_only')
        self.assertFalse(item['delete_safe'])

    def test_sizes_deduplicate_inodes_but_never_promise_physical_reclaim(self):
        data = self.root / 'object'
        data.write_bytes(b'a' * 4096)
        os.link(data, self.root / 'object-link')
        os.link(data, self.base / 'outside-link')
        info = data.stat()
        size = self.item(sizes=True)['sizes']
        self.assertEqual(size['status'], 'complete')
        self.assertEqual(size['nominal_regular_bytes'], 8192)
        self.assertEqual(size['allocated_regular_bytes'], info.st_blocks * 512 * 2)
        self.assertEqual(size['unique_inode_allocated_bytes'], info.st_blocks * 512)
        self.assertEqual(size['shared_hardlink_allocated_bytes'], info.st_blocks * 512)
        self.assertIsNone(size['physically_reclaimable_bytes'])
        self.assertIsNone(self.report(sizes=True)['size_totals'])

    def test_size_scan_does_not_follow_symlinks_or_open_native_or_config_files(self):
        private = self.root / '.harness/codex/sessions/body.jsonl'
        private.parent.mkdir(parents=True)
        private.write_text('PRIVATE_BODY_SENTINEL')
        (self.root / 'escape').symlink_to(self.source, target_is_directory=True)
        (self.source / 'big').write_bytes(b'x' * 8192)
        module = self.module()
        opened = []
        original = module.os.open
        def observe(path, *args, **kwargs):
            opened.append(os.fspath(path))
            return original(path, *args, **kwargs)
        with patch.object(module.os, 'open', side_effect=observe):
            item = module.build_report(state=self.state, runtime_home=self.runtime, now=NOW, sizes=True)['sessions'][0]
        self.assertNotIn('body.jsonl', opened)
        self.assertNotIn('big', opened)
        self.assertEqual(item['sizes']['nominal_regular_bytes'], len('PRIVATE_BODY_SENTINEL'))
        self.assertEqual(item['sizes']['symlinks_skipped'], 1)

    def test_budget_and_inaccessible_tree_have_null_totals_not_zero(self):
        for n in range(20):
            (self.root / f'file{n}').write_bytes(b'x')
        item = self.item(sizes=True, max_nodes=12)
        self.assertEqual(item['sizes']['status'], 'partial')
        self.assertIsNone(item['sizes']['nominal_regular_bytes'])
        self.assertIn('scan_incomplete', item['retained_reasons'])
        module = self.module()
        original = module.os.scandir
        def denied(path):
            if isinstance(path, int) and os.fstat(path).st_ino == self.root.stat().st_ino:
                raise PermissionError('synthetic tree denial')
            return original(path)
        with patch.object(module.os, 'scandir', side_effect=denied):
            item = module.build_report(state=self.state, runtime_home=self.runtime, now=NOW, sizes=True)['sessions'][0]
        self.assertEqual(item['sizes']['status'], 'unknown')
        self.assertIsNone(item['sizes']['allocated_regular_bytes'])

    def test_missing_or_symlink_state_is_unknown_and_does_not_create_it(self):
        missing = self.base / 'missing-state'
        result = self.cli(state=missing)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['scan_status'], 'unknown')
        self.assertIn('state_unknown', report['problems'])
        self.assertFalse(missing.exists())
        linked = self.base / 'linked-state'
        linked.symlink_to(self.state, target_is_directory=True)
        self.assertEqual(json.loads(self.cli(state=linked).stdout)['scan_status'], 'unknown')

    def test_archive_content_is_never_opened_and_verification_stays_unknown(self):
        archive = self.state / 'archives' / ID
        archive.mkdir(parents=True)
        (archive / 'manifest.json').write_text('{"files":[]}')
        (archive / 'transcript.jsonl').write_text('PRIVATE_BODY_SENTINEL')
        before = self.snapshot()
        item = self.item()
        self.assertEqual(item['archive']['presence'], 'present')
        self.assertEqual(item['archive']['verification'], 'unknown')
        self.assertEqual(before, self.snapshot())

    def test_gc_range_and_archive_retention_compatibility_and_bad_args(self):
        self.assertEqual(self.cli(retention='archive').returncode, 0)
        self.assertEqual(json.loads(self.cli(retention='archive').stdout)['retention_seconds'], 86400)
        for retention in ('-1', '604801', 'nonsense'):
            self.assertEqual(self.cli(retention=retention).returncode, 2)
        self.assertEqual(self.cli('--delete').returncode, 2)
        self.assertEqual(self.cli('--max-nodes', '0').returncode, 2)

    def test_session_listing_is_bounded_and_reports_unreadable_records(self):
        (self.state / 'sessions' / OTHER).symlink_to(self.record, target_is_directory=True)
        report = self.report(max_sessions=1)
        self.assertEqual(report['scan_status'], 'partial')
        self.assertIn('session_limit', report['problems'])
        report = self.report()
        unsafe = next(item for item in report['sessions'] if item['session_id'] == OTHER)
        self.assertIn('record_unknown', unsafe['retained_reasons'])

    def test_delivered_and_discarded_require_terminal_evidence(self):
        self.journal(state='DELIVERED', identity='e' * 64)
        self.assertIn('terminal_evidence_unknown', self.item()['retained_reasons'])
        (self.record / 'delivered-sha').write_text('f' * 40 + '\n')
        (self.record / 'delivered-manifest').write_bytes(b'F\x00100644\x00' + b'f' * 40 + b'\x00file\x00')
        self.assertNotIn('terminal_evidence_unknown', self.item()['retained_reasons'])
        self.journal(state='DISCARDED')
        self.assertIn('terminal_evidence_unknown', self.item()['retained_reasons'])
        (self.record / 'discarded.patch').write_text('must not read this patch body')
        self.assertNotIn('terminal_evidence_unknown', self.item()['retained_reasons'])


    def test_root_replaced_after_validation_is_not_measured_as_trusted_root(self):
        module = self.module()
        original = module.holds
        def replace(*args):
            result = original(*args)
            self.root.rename(self.base / 'original-root')
            self.root.mkdir()
            (self.root / 'replacement-file').write_bytes(b'x' * 512)
            return result
        with patch.object(module, 'holds', side_effect=replace):
            item = module.build_report(state=self.state, runtime_home=self.runtime, now=NOW, sizes=True)['sessions'][0]
        self.assertEqual(item['sizes']['status'], 'unknown')
        self.assertIsNone(item['sizes']['nominal_regular_bytes'])
        self.assertIn('root_identity_mismatch', item['retained_reasons'])

    def test_deeply_nested_json_is_unknown_without_crashing(self):
        deep = '[' * 2000 + '0' + ']' * 2000
        for path in (self.record / 'resume-hold-deep.json', self.runtime / 'current.json',
                     self.runtime / 'bindings' / f'{ID}.json'):
            path.write_text(deep)
        item = self.item()
        self.assertIn('resume_hold_unknown', item['retained_reasons'])
        self.assertIn('bound_runtime_unknown', item['retained_reasons'])
        self.assertIn('selected_runtime_unknown', item['retained_reasons'])

    def test_deadline_stops_following_sessions_and_marks_coverage_incomplete(self):
        (self.state / 'sessions' / OTHER).mkdir()
        module = self.module()
        clock = [0.0]
        original = module.metadata
        def advance(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if path.name == 'source-root':
                clock[0] = 2.0
            return result
        with patch.object(module.time, 'monotonic', side_effect=lambda: clock[0]), patch.object(module, 'metadata', side_effect=advance):
            report = module.build_report(state=self.state, runtime_home=self.runtime, now=NOW, max_seconds=1)
        self.assertEqual(len(report['sessions']), 1)
        self.assertEqual(report['scan_status'], 'partial')
        self.assertIn('time_budget', report['problems'])

    def test_metadata_only_runtime_proof_keeps_provenance_explicitly_unverified(self):
        item = self.item()
        self.assertIn('needs_provenance_recovery', item['retained_reasons'])
        self.assertEqual(item['provenance_verification'], 'unknown')
        self.assertFalse(item['delete_safe'])

    def test_size_scan_never_opens_any_regular_file_body(self):
        for relative in ('.harness/codex/auth.json', '.harness/codex/config.toml',
                         '.harness/codex/sessions/rollout.jsonl', '.git/config'):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('PRIVATE_BODY_SENTINEL')
        archive = self.state / 'archives' / ID
        archive.mkdir(parents=True)
        (archive / 'manifest.json').write_text('PRIVATE_BODY_SENTINEL')
        module = self.module()
        original = module.os.open
        allowed = {'journal', 'session-root', 'source-root', 'base-sha', 'root-inode',
                   'lease-v1', 'runtime.lock', 'current.json', f'{ID}.json'}
        def guarded(path, flags, *args, **kwargs):
            if not flags & os.O_DIRECTORY:
                self.assertIn(os.fspath(path), allowed)
                self.assertEqual(flags & os.O_ACCMODE, os.O_RDONLY)
                self.assertFalse(flags & (os.O_CREAT | os.O_TRUNC))
            return original(path, flags, *args, **kwargs)
        with patch.object(module.os, 'open', side_effect=guarded):
            report = module.build_report(state=self.state, runtime_home=self.runtime, now=NOW, sizes=True)
        self.assertNotIn('PRIVATE_BODY_SENTINEL', json.dumps(report))

    def test_fifo_metadata_is_unknown_without_blocking(self):
        (self.record / 'source-root').unlink()
        os.mkfifo(self.record / 'source-root')
        self.assertIn('source_unknown', self.item()['retained_reasons'])


    def test_same_session_deadline_stops_remaining_holds_and_runtime_reads(self):
        for name in ('a', 'b', 'c'):
            (self.record / f'resume-hold-{name}.json').write_text(json.dumps({
                'pid': 123, 'start': 'synthetic start', 'source_root': str(self.source),
                'session_id': ID, 'request_id': name}))
        module = self.module()
        clock, reads = [0.0], []
        original = module.metadata
        def expire_after_first_hold(path, *args, **kwargs):
            reads.append(path.name)
            result = original(path, *args, **kwargs)
            if path.name == 'resume-hold-a.json':
                clock[0] = 2.0
            return result
        with patch.object(module.time, 'monotonic', side_effect=lambda: clock[0]), patch.object(module, 'metadata', side_effect=expire_after_first_hold):
            report = module.build_report(state=self.state, runtime_home=self.runtime, now=NOW, max_seconds=1)
        self.assertEqual(reads[-1], 'resume-hold-a.json', reads)
        self.assertNotIn('resume-hold-b.json', reads)
        self.assertNotIn('resume-hold-c.json', reads)
        self.assertNotIn('current.json', reads)
        self.assertNotIn(f'{ID}.json', reads)
        self.assertEqual(report['scan_status'], 'partial')
        self.assertIn('scan_budget', report['problems'])
        item = report['sessions'][0]
        self.assertFalse(item['holds']['scan_complete'])
        self.assertEqual(item['runtime']['bound'], None)
        self.assertEqual(item['runtime']['selected'], None)
        self.assertEqual(item['archive']['presence'], 'unknown')
        self.assertIn('scan_incomplete', item['retained_reasons'])

    def test_final_session_deadline_after_each_metadata_stage_is_partial(self):
        for trigger in ('journal', 'source-root', 'lease-v1', 'current.json', f'{ID}.json'):
            with self.subTest(trigger=trigger):
                module = self.module()
                clock, late_reads = [0.0], []
                original = module.metadata
                def advance(path, *args, **kwargs):
                    if clock[0] >= 1:
                        late_reads.append(path.name)
                    result = original(path, *args, **kwargs)
                    if path.name == trigger:
                        clock[0] = 2.0
                    return result
                with patch.object(module.time, 'monotonic', side_effect=lambda: clock[0]), patch.object(module, 'metadata', side_effect=advance):
                    report = module.build_report(state=self.state, runtime_home=self.runtime, now=NOW, max_seconds=1, sizes=True)
                self.assertEqual(late_reads, [])
                self.assertEqual(report['scan_status'], 'partial')
                self.assertIn('scan_budget', report['problems'])
                self.assertIn('scan_incomplete', report['sessions'][0]['retained_reasons'])


if __name__ == '__main__':
    unittest.main()
