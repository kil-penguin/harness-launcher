#!/usr/bin/env python3
"""Additional failure-report controls against real same synthetic backend.

Catches omitted captured second preflight record, post-effect mode/owner drift,
final-check stale earlier-target claims, and descriptor-close exception leakage.
"""
import importlib.util
from pathlib import Path
import os
import stat
import unittest

P=Path(__file__).resolve().parent/'test_omp_ci_owned_homebrew.py'
spec=importlib.util.spec_from_file_location('base_owned_tests',P)
base=importlib.util.module_from_spec(spec);spec.loader.exec_module(base)

class Extra(base.Preparation):
    def test_both_before_records_retained_on_first_owner_rejection(self):
        self.fs.owner={'homebrew':os.getuid()+1}
        code,r=self.run_case();self.assertNotEqual(code,0)
        self.assertIsNotNone(r['targets'][0]['before'])
        self.assertIsNotNone(r['targets'][1]['before'])
        self.assertEqual(self.fs.writes,[])
    def test_owner_drift_before_first_write_refused(self):
        original=self.fs.named
        def named(name,parent):
            if name=='homebrew' and self.fs.named_counts.get('homebrew',0)==1:
                self.fs.owner['homebrew']=os.getuid()+1
            return original(name,parent)
        self.fs.named=named
        code,r=self.run_case();self.assertNotEqual(code,0);self.assertEqual(self.fs.writes,[])
    def test_successful_syscall_wrong_mode_is_failure(self):
        original=self.fs.chmod
        def chmod(fd,mode):original(fd,mode | 0o020)
        self.fs.chmod=chmod
        code,r=self.run_case();self.assertNotEqual(code,0);self.assertEqual(len(self.fs.writes),1)
        self.assertEqual(r['targets'][0]['after']['mode'],0o775)
    def test_owner_change_after_syscall_is_failure(self):
        original=self.fs.chmod
        def chmod(fd,mode):
            original(fd,mode);self.fs.owner['homebrew']=os.getuid()+1
        self.fs.chmod=chmod
        code,r=self.run_case();self.assertNotEqual(code,0);self.assertTrue(r['targets'][0]['changed'])
    def test_root_unsafe_parent_refused(self):
        self.fs.owner={'homebrew':0};code,r=self.run_case()
        self.assertNotEqual(code,0);self.assertEqual(self.fs.writes,[])
    def test_all_special_mode_bits_preserved(self):
        os.chmod(self.parent,0o7775)
        code,r=self.run_case();self.assertEqual(code,0)
        self.assertEqual(stat.S_IMODE(self.parent.stat().st_mode),0o7755)
    def test_final_drift_reports_actual_earlier_target(self):
        original=self.fs.named
        def named(name,parent):
            if name=='homebrew' and self.fs.named_counts.get('homebrew',0)==5:
                os.chmod(self.parent,0o775)
            return original(name,parent)
        self.fs.named=named
        code,r=self.run_case();self.assertNotEqual(code,0)
        self.assertEqual(r['targets'][0]['after']['mode'],0o775)
        self.assertEqual(r['targets'][0]['status'],'failed')
    def test_close_error_is_closed_failure_not_exception(self):
        original=self.fs.close;failed=False
        def close(fd):
            nonlocal failed
            original(fd)
            if not failed:failed=True;raise OSError('PRIVATE close detail')
        self.fs.close=close
        code,r=self.run_case();self.assertNotEqual(code,0)
        self.assertEqual(r['error'],'CLOSE_FAILED')
        for fd in self.fs.fds:
            with self.assertRaises(OSError):os.fstat(fd)

# Do not inherit/run the original26 twice in this supplemental suite.
for name in list(base.Preparation.__dict__):
    if name.startswith('test_') and name not in Extra.__dict__:setattr(Extra,name,None)
if __name__=='__main__':unittest.main(verbosity=2)
