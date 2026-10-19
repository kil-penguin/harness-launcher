#!/usr/bin/env python3
"""Owned synthetic filesystem/injected-observation proof; never opens real Homebrew.
Breaks caught: wider mutation, late ownership admission, stale binding, mode widening,
partial-success claims, exception leakage, and accidental local/target CLI admission.
"""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
HELPER = HERE / 'omp-ci-owned-homebrew.py'
SPEC = importlib.util.spec_from_file_location('ci_owned_homebrew', HELPER) if HELPER.exists() else None
M = importlib.util.module_from_spec(SPEC) if SPEC else None
if M:
    SPEC.loader.exec_module(M)
CONTEXT = {'GITHUB_ACTIONS': 'true', 'RUNNER_ENVIRONMENT': 'github-hosted', 'RUNNER_OS': 'macOS'}
TARGETS = ['/opt/homebrew', '/opt/homebrew/Cellar']

class SyntheticFS:
    """Real descriptor operations rooted only in this test's owned temporary tree.

    UID/race/error injections are observations, not a fake executable or guard.
    """
    def __init__(self, root):
        self.root = root
        self.fds = {}
        self.opens = 0
        self.writes = []
        self.named_counts = {}
        self.owner = {}
        self.process_uid = os.getuid()
        self.fail_write = 0
        self.raise_after_write = False
        self.replace_before = False
        self.replace_after = False
        self.unavailable_after = False
        self.mode_drift = False
        self.fstat_drift = False
    def uid(self):
        return self.process_uid
    def open_root(self):
        self.opens += 1
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self.fds[fd] = self.root
        return fd
    def open_dir(self, name, parent):
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        self.fds[fd] = self.fds[parent] / name
        return fd
    def observed(self, info, path):
        values = list(info)
        if path.name in self.owner:
            values[4] = self.owner[path.name]
        return os.stat_result(values)
    def named(self, name, parent):
        p = self.root if parent is None else self.fds[parent] / name
        self.named_counts[p.name] = self.named_counts.get(p.name, 0) + 1
        count = self.named_counts[p.name]
        if p.name == 'homebrew' and count == 2:
            if self.replace_before:
                p.rename(p.with_name('old-homebrew'))
                p.mkdir(mode=0o755)
            if self.mode_drift:
                os.chmod(p, 0o777)
        return self.observed(os.stat(p, follow_symlinks=False), p)
    def stat(self, fd):
        p = self.fds[fd]
        if self.unavailable_after and self.writes and p.name == 'homebrew':
            raise OSError('PRIVATE injected detail must not escape')
        info = self.observed(os.fstat(fd), p)
        if self.fstat_drift and p.name == 'Cellar':
            values = list(info); values[1] += 1
            return os.stat_result(values)
        return info
    def chmod(self, fd, mode):
        self.writes.append((self.fds[fd].name, mode))
        if self.fail_write == len(self.writes) and not self.raise_after_write:
            raise OSError('PRIVATE injected detail must not escape')
        os.fchmod(fd, mode)
        if self.replace_after and len(self.writes) == 1:
            p = self.fds[fd]
            p.rename(p.with_name('old-homebrew'))
            p.mkdir(mode=0o755)
        if self.fail_write == len(self.writes) and self.raise_after_write:
            raise OSError('PRIVATE injected detail must not escape')
    def close(self, fd):
        os.close(fd)

class Preparation(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(M, 'missing accepted owner-only CI preparation helper')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'opt/homebrew/Cellar').mkdir(parents=True)
        self.parent = self.root / 'opt/homebrew'
        self.cellar = self.parent / 'Cellar'
        for p in (self.parent, self.cellar): os.chmod(p, 0o775)
        self.content = self.cellar / 'sentinel'
        self.content.write_bytes(b'unchanged synthetic content\x00')
        self.fs = SyntheticFS(self.root)
    def run_case(self, context=None):
        code, report = M.prepare(CONTEXT if context is None else context, self.fs)
        self.assertEqual([r['path'] for r in report['targets']], TARGETS)
        self.assertLess(len(json.dumps(report)), 8192)
        self.assertNotIn('PRIVATE', json.dumps(report))
        self.assertEqual(set(report), {'schema', 'status', 'error', 'targets'})
        self.assertEqual(code == 0, report['status'] == 'prepared')
        return code, report
    def test_owned_0775_becomes_0755_exact_two_only(self):
        before = self.content.stat(); code, r = self.run_case()
        self.assertEqual(code, 0); self.assertEqual(self.fs.writes, [('homebrew', 0o755), ('Cellar', 0o755)])
        self.assertEqual([stat.S_IMODE(p.stat().st_mode) for p in (self.parent,self.cellar)], [0o755,0o755])
        self.assertEqual([x['changed'] for x in r['targets']], [True,True])
        self.assertEqual(self.content.read_bytes(), b'unchanged synthetic content\x00')
        self.assertEqual(self.content.stat().st_ino,before.st_ino)
    def test_safe_owned_noop(self):
        for p in (self.parent,self.cellar): os.chmod(p,0o755)
        code,r=self.run_case();self.assertEqual(code,0);self.assertEqual(self.fs.writes,[])
        self.assertEqual([x['changed'] for x in r['targets']],[False,False])
    def test_special_bits_owner_and_contents_preserved(self):
        os.chmod(self.parent,0o3775); before=self.parent.stat()
        code,r=self.run_case(); self.assertEqual(code,0)
        self.assertEqual(stat.S_IMODE(self.parent.stat().st_mode),0o3755)
        self.assertEqual((self.parent.stat().st_uid,self.parent.stat().st_gid),(before.st_uid,before.st_gid))
        self.assertEqual(self.content.read_bytes(),b'unchanged synthetic content\x00')
    def test_root_safe_noop(self):
        self.fs.owner={'homebrew':0,'Cellar':0}
        for p in (self.parent,self.cellar):os.chmod(p,0o755)
        code,r=self.run_case();self.assertEqual(code,0);self.assertEqual(self.fs.writes,[])
    def test_root_safe_parent_owned_unsafe_cellar(self):
        self.fs.owner={'homebrew':0};os.chmod(self.parent,0o755)
        code,r=self.run_case();self.assertEqual(code,0);self.assertEqual(self.fs.writes,[('Cellar',0o755)])
    def test_root_unsafe_second_preflight_blocks_first_write(self):
        self.fs.owner={'Cellar':0};code,r=self.run_case()
        self.assertNotEqual(code,0);self.assertEqual(self.fs.writes,[])
        self.assertEqual(r['error'],'ROOT_UNSAFE')
        self.assertEqual(stat.S_IMODE(self.parent.stat().st_mode),0o775)
    def test_foreign_safe_second_blocks_first_write(self):
        self.fs.owner={'Cellar':os.getuid()+1};os.chmod(self.cellar,0o755)
        code,r=self.run_case();self.assertNotEqual(code,0);self.assertEqual(self.fs.writes,[])
        self.assertEqual(r['error'],'FOREIGN_OWNER')
    def test_foreign_unsafe_first_refuses(self):
        self.fs.owner={'homebrew':os.getuid()+1};code,r=self.run_case()
        self.assertNotEqual(code,0);self.assertEqual(self.fs.writes,[])
    def test_root_process_cannot_mutate_even_owned(self):
        self.fs.process_uid=0;self.fs.owner={'homebrew':0,'Cellar':0}
        code,r=self.run_case();self.assertNotEqual(code,0);self.assertEqual(self.fs.writes,[])
    def test_wrong_context_never_opens_root(self):
        for key,value in [('GITHUB_ACTIONS','false'),('RUNNER_ENVIRONMENT','self-hosted'),('RUNNER_OS','Linux')]:
            context=dict(CONTEXT);context[key]=value
            code,r=self.run_case(context);self.assertNotEqual(code,0);self.assertEqual(self.fs.opens,0)
    def test_missing_context_never_opens_root(self):
        code,r=self.run_case({});self.assertNotEqual(code,0);self.assertEqual(self.fs.opens,0)
    def test_symlink_target_rejected_without_writes(self):
        self.content.unlink();self.cellar.rmdir();self.cellar.symlink_to(self.root/'opt',target_is_directory=True)
        code,r=self.run_case();self.assertNotEqual(code,0);self.assertEqual(self.fs.writes,[])
    def test_symlink_ancestor_rejected_without_writes(self):
        (self.root/'opt').rename(self.root/'other');(self.root/'opt').symlink_to(self.root/'other',target_is_directory=True)
        code,r=self.run_case();self.assertNotEqual(code,0);self.assertEqual(self.fs.writes,[])
    def test_missing_second_directory_rejected_before_writes(self):
        self.content.unlink();self.cellar.rmdir();code,r=self.run_case()
        self.assertNotEqual(code,0);self.assertEqual(self.fs.writes,[])
    def test_regular_file_target_rejected(self):
        self.content.unlink();self.cellar.rmdir();self.cellar.write_bytes(b'content')
        code,r=self.run_case();self.assertNotEqual(code,0);self.assertEqual(self.fs.writes,[])
    def test_initial_named_descriptor_identity_mismatch(self):
        self.fs.fstat_drift=True;code,r=self.run_case()
        self.assertNotEqual(code,0);self.assertEqual(self.fs.writes,[]);self.assertEqual(r['error'],'BINDING_CHANGED')
    def test_replaced_named_parent_before_first_write(self):
        self.fs.replace_before=True;code,r=self.run_case()
        self.assertNotEqual(code,0);self.assertEqual(self.fs.writes,[]);self.assertEqual(r['error'],'BINDING_CHANGED')
    def test_unexpected_mode_before_write_not_normalized(self):
        self.fs.mode_drift=True;code,r=self.run_case()
        self.assertNotEqual(code,0);self.assertEqual(self.fs.writes,[]);self.assertEqual(r['error'],'BINDING_CHANGED')
    def test_replaced_named_parent_after_first_write(self):
        self.fs.replace_after=True;code,r=self.run_case()
        self.assertNotEqual(code,0);self.assertEqual(len(self.fs.writes),1)
        self.assertTrue(r['targets'][0]['changed']);self.assertEqual(r['targets'][1]['status'],'unattempted')
    def test_first_write_error_no_retry(self):
        self.fs.fail_write=1;code,r=self.run_case()
        self.assertNotEqual(code,0);self.assertEqual(len(self.fs.writes),1)
        self.assertFalse(r['targets'][0]['changed']);self.assertEqual(r['targets'][1]['status'],'unattempted')
    def test_second_write_error_records_partial_change_no_rollback(self):
        self.fs.fail_write=2;code,r=self.run_case()
        self.assertNotEqual(code,0);self.assertEqual(len(self.fs.writes),2)
        self.assertEqual([x['changed'] for x in r['targets']],[True,False])
        self.assertEqual([stat.S_IMODE(p.stat().st_mode) for p in (self.parent,self.cellar)],[0o755,0o775])
    def test_error_after_applied_write_reports_change(self):
        self.fs.fail_write=1;self.fs.raise_after_write=True;code,r=self.run_case()
        self.assertNotEqual(code,0);self.assertTrue(r['targets'][0]['changed']);self.assertEqual(len(self.fs.writes),1)
    def test_unavailable_poststat_does_not_claim_unchanged_or_success(self):
        self.fs.unavailable_after=True;code,r=self.run_case()
        self.assertNotEqual(code,0);self.assertIsNone(r['targets'][0]['changed'])
        self.assertEqual(r['targets'][0]['status'],'unavailable')
    def test_descriptors_closed(self):
        self.run_case()
        for fd in self.fs.fds:
            with self.assertRaises(OSError):os.fstat(fd)
    def test_cli_arbitrary_target_refused_without_effect(self):
        proc=subprocess.run([sys.executable,'-I','-S','-B',str(HELPER),str(self.parent)],env={'PATH':'/usr/bin:/bin'},capture_output=True)
        self.assertEqual(proc.returncode,2);self.assertEqual(stat.S_IMODE(self.parent.stat().st_mode),0o775)
        self.assertEqual(json.loads(proc.stdout)['error'],'ARGUMENTS_REFUSED')
    def test_cli_local_context_refused_without_effect(self):
        proc=subprocess.run([sys.executable,'-I','-S','-B',str(HELPER)],env={'PATH':'/usr/bin:/bin'},capture_output=True)
        self.assertEqual(proc.returncode,2);self.assertEqual(json.loads(proc.stdout)['error'],'CONTEXT_REFUSED')

if __name__=='__main__':unittest.main(verbosity=2)
