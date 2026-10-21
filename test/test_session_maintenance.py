"""Exercise the maintenance worker against owned real isolation/GC fixtures."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
import test_session_gc_safety as fixture
ROOT,ENGINE=fixture.ROOT,fixture.ENGINE


class MaintenanceTests(unittest.TestCase):
    setUp=fixture.GCSafetyTests.setUp
    engine=fixture.GCSafetyTests.engine
    closed=fixture.GCSafetyTests.closed

    def sweep(self,apply=False,limit=10):
        scopes=[{'profile':'fixture','source_root':str(self.source),'state_root':str(self.state)}]
        program='''
import sys,json
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from harness_session_maintenance import sweep
result=sweep(Path(sys.argv[2]),json.loads(sys.argv[3]),Path(sys.argv[4]),
             apply=sys.argv[5]=='1',max_owners=int(sys.argv[6]))
print(json.dumps(result))
'''
        result=subprocess.run([sys.executable,'-I','-B','-c',program,str(ROOT/'bin'),str(ENGINE),
            json.dumps(scopes),str(self.base/'diagnostics'),'1' if apply else '0',str(limit)],
            capture_output=True,text=True,timeout=120)
        self.assertEqual(result.returncode,0,result.stderr)
        return json.loads(result.stdout)

    def expire(self,owner):
        journal=self.state/'sessions'/owner/'journal'
        text=journal.read_text();lines=text.splitlines()
        journal.write_text('\n'.join('heartbeat=2000-01-01T00:00:00Z' if row.startswith('heartbeat=') else row for row in lines)+'\n')

    def test_default_report_only_preserves_workspaces_and_publishes_private_report(self):
        owner,root=self.closed();self.expire(owner)
        result=self.sweep()
        self.assertTrue(root.exists());self.assertEqual(result['mode'],'report-only')
        self.assertEqual(result['retired'],[])
        before=Path(result['before_report']);self.assertTrue(before.is_file())
        self.assertEqual(before.stat().st_mode&0o777,0o600)
        report=json.loads(before.read_text());self.assertEqual(report['candidates'][0]['session_id'],owner)

    def test_apply_respects_owner_cap_and_has_durable_report_before_real_fixture_gc(self):
        owner,root=self.closed();other,other_root=self.closed()
        self.expire(owner);self.expire(other)
        result=self.sweep(apply=True,limit=1)
        self.assertEqual(len(result['retired']),1)
        self.assertEqual(sum(path.exists() for path in (root,other_root)),1)
        report=Path(result['before_report']);self.assertTrue(report.is_file())
        retired=result['retired'][0];archive=Path(retired['archive'])
        self.assertTrue(archive.is_file())
        self.assertLessEqual(report.stat().st_mtime_ns,archive.stat().st_mtime_ns)


if __name__=='__main__':unittest.main()
