"""GC must preserve changes made after CLOSED or after a delivered receipt."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import shutil
import unittest

ROOT=Path(__file__).resolve().parents[1]
ID='11111111-2222-4333-8444-555555555555'


class WorkProofTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name).resolve();self.source=self.base/'source'
        self.source.mkdir();self.git(self.source,'init','-q','-b','main')
        self.git(self.source,'config','user.name','Fixture');self.git(self.source,'config','user.email','qa@example.invalid')
        (self.source/'tracked').write_text('base\n');(self.source/'.gitignore').write_text('ignored\n.harness/\n')
        self.git(self.source,'add','.');self.git(self.source,'commit','-qm','base')
        self.sha=self.git(self.source,'rev-parse','HEAD');self.state=self.base/'state';self.root=self.state/'worktrees'/ID
        self.root.parent.mkdir(parents=True);self.git(self.source,'clone','-q','--no-checkout',str(self.source),str(self.root))
        self.git(self.root,'checkout','-q','--detach',self.sha)
        self.record=self.state/'sessions'/ID;self.record.mkdir(parents=True)
        for name,value in {'source-root':str(self.source),'session-root':str(self.root),'base-sha':self.sha,
                           'root-inode':f'{self.root.stat().st_dev} {self.root.stat().st_ino}',
                           'journal':'state=CLOSED\nidentity=\nheartbeat=2000-01-01T00:00:00Z'}.items():
            (self.record/name).write_text(value+'\n')

    def git(self,root,*args):
        return subprocess.check_output(['git','-c','core.hooksPath=/dev/null','-C',str(root),*args],text=True,stderr=subprocess.DEVNULL).strip()

    def proof(self,expected):
        before=(self.root/'.git/index').read_bytes()
        done=subprocess.run([sys.executable,'-I','-B',str(ROOT/'bin/harness_session_work_proof.py'),
            '--state',str(self.state),'--owner',ID],capture_output=True,text=True,timeout=30)
        self.assertEqual(done.returncode,expected,done.stderr)
        self.assertEqual(json.loads(done.stdout)['status'],'proven' if expected==0 else 'retained')
        self.assertEqual((self.root/'.git/index').read_bytes(),before)

    def test_clean_closed_root_is_proven_without_running_session_configuration(self):
        sentinel=self.base/'session-hook-executed'
        hook=self.base/'hook';hook.write_text('#!/bin/sh\ntouch '+str(sentinel)+'\n');hook.chmod(0o700)
        self.git(self.root,'config','core.fsmonitor',str(hook))
        self.proof(0);self.assertFalse(sentinel.exists())

    def test_closed_root_with_new_tracked_or_ignored_work_is_retained(self):
        (self.root/'tracked').write_text('later work\n');self.proof(2)
        (self.root/'tracked').write_text('base\n');(self.root/'ignored').write_text('only copy of ignored work\n');self.proof(2)

    def test_delivered_content_is_accepted_but_later_edits_are_retained(self):
        (self.source/'tracked').write_text('delivered\n');self.git(self.source,'add','tracked');self.git(self.source,'commit','-qm','delivered')
        delivered=self.git(self.source,'rev-parse','HEAD');oid=self.git(self.source,'rev-parse','HEAD:tracked')
        (self.record/'delivered-sha').write_text(delivered+'\n')
        (self.record/'delivered-manifest').write_bytes(b'F\x00100644\x00'+oid.encode()+b'\x00tracked\x00')
        (self.record/'journal').write_text('state=DELIVERED\nidentity='+'a'*64+'\nheartbeat=2000-01-01T00:00:00Z\n')
        (self.root/'tracked').write_text('delivered\n');self.git(self.root,'add','tracked')
        self.proof(0)
        (self.root/'tracked').write_text('not delivered\n');self.proof(2)

    def test_staged_only_change_is_retained(self):
        (self.root/'tracked').write_text('staged only\n');self.git(self.root,'add','tracked')
        (self.root/'tracked').write_text('base\n');self.proof(2)

    def test_discard_patch_accounts_for_committed_binary_and_untracked_work(self):
        (self.root/'tracked').write_text('discarded\n');self.git(self.root,'add','tracked')
        self.git(self.root,'-c','user.name=Fixture','-c','user.email=qa@example.invalid','commit','-qm','discarded commit')
        (self.root/'binary').write_bytes(b'\x00unique binary\x01');self.git(self.root,'add','binary')
        patch=subprocess.check_output(['git','-C',str(self.root),'diff','--cached','--binary',self.sha])
        (self.record/'discarded.patch').write_bytes(patch)
        (self.record/'journal').write_text('state=DISCARDED\nidentity=\nheartbeat=2000-01-01T00:00:00Z\n')
        before=self.git(self.source,'count-objects','-v');self.proof(0)
        self.assertEqual(self.git(self.source,'count-objects','-v'),before)
        (self.root/'binary').write_bytes(b'\x00later unique\x01');self.proof(2)

    def test_staged_only_deletion_after_discard_or_in_headless_index_is_retained(self):
        for lifecycle in ('DISCARDED','CLOSED'):
            with self.subTest(lifecycle=lifecycle):
                self.git(self.root,'reset','--mixed',self.sha)
                (self.record/'journal').write_text('state='+lifecycle+'\nidentity=\nheartbeat=2000-01-01T00:00:00Z\n')
                (self.record/'discarded.patch').write_bytes(b'')
                if lifecycle=='CLOSED':
                    (self.record/'headless').touch();trusted=self.record/'trusted.git';trusted.mkdir()
                    shutil.copyfile(self.root/'.git/index',trusted/'index')
                self.proof(0)
                self.git(self.root,'rm','--cached','tracked')
                self.proof(2)


if __name__=='__main__':unittest.main()
