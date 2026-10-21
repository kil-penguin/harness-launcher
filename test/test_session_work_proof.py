"""GC must preserve changes made after CLOSED or after a delivered receipt."""
import hashlib
import json
import os
import py_compile
from pathlib import Path
import subprocess
import sys
import tempfile
import shutil
import unittest
from unittest import mock
import runpy

ROOT=Path(__file__).resolve().parents[1]
ID='11111111-2222-4333-8444-555555555555'


class WorkProofTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name).resolve();self.source=self.base/'source'
        self.source.mkdir();self.git(self.source,'init','-q','-b','main')
        self.git(self.source,'config','user.name','Fixture');self.git(self.source,'config','user.email','qa@example.invalid')
        (self.source/'tracked').write_text('base\n');(self.source/'.gitignore').write_text('ignored\n.harness/\n__pycache__/\n')
        (self.source/'pkg').mkdir();(self.source/'pkg/module.py').write_text('VALUE = 1\n')
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

    def proof(self,expected,preserve=False):
        before=(self.root/'.git/index').read_bytes()
        args=[sys.executable,'-I','-B',str(ROOT/'bin/harness_session_work_proof.py'),
              '--state',str(self.state),'--owner',ID]
        if preserve:args.append('--preserve-python-caches')
        done=subprocess.run(args,capture_output=True,text=True,timeout=30)
        self.assertEqual(done.returncode,expected,done.stderr)
        self.assertEqual(json.loads(done.stdout)['status'],'proven' if expected==0 else 'retained')
        self.assertEqual((self.root/'.git/index').read_bytes(),before)
        return json.loads(done.stdout)

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

    def cache(self, optimization=0, invalidation_mode=None):
        source=self.root/'pkg/module.py'
        return Path(py_compile.compile(str(source),doraise=True,optimize=optimization,invalidation_mode=invalidation_mode))

    def test_generated_bytecode_beside_accounted_python_source_does_not_block_proof(self):
        for optimization in (0,1,2):
            self.cache(optimization)
        self.proof(0,preserve=True)

    def test_source_hash_bytecode_is_proven_and_stale_hash_is_retained(self):
        for mode in (py_compile.PycInvalidationMode.CHECKED_HASH,py_compile.PycInvalidationMode.UNCHECKED_HASH):
            with self.subTest(mode=mode):
                cache=self.cache(invalidation_mode=mode);self.proof(0,preserve=True)
                raw=bytearray(cache.read_bytes());raw[8]^=1;cache.write_bytes(raw);self.proof(2)

    def test_fake_corrupt_or_stale_cache_is_retained_despite_valid_name(self):
        cache=self.cache(invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP);raw=cache.read_bytes()
        mutations={'text':b'only copy of notes','truncated':raw[:12],
                   'magic':b'bad!'+raw[4:],'flags':raw[:4]+(4).to_bytes(4,'little')+raw[8:],
                   'stale_timestamp':raw[:8]+b'\0\0\0\0'+raw[12:],
                   'stale_size':raw[:12]+b'\0\0\0\0'+raw[16:],
                   'body':raw[:16]+b'private work'}
        for name,value in mutations.items():
            with self.subTest(name=name):cache.write_bytes(value);self.proof(2,preserve=True)

    def test_unsupported_cpython_cache_tag_is_retained(self):
        cache=self.cache();foreign=cache.parent/'module.cpython-999.pyc'
        shutil.copyfile(cache,foreign);self.proof(2)

    def test_generated_cache_is_proven_after_gc_renames_the_same_root(self):
        self.cache();tomb=self.root.with_name('.archiving-'+ID+'-123')
        self.root.rename(tomb)
        done=subprocess.run([sys.executable,'-I','-B',str(ROOT/'bin/harness_session_work_proof.py'),
            '--state',str(self.state),'--owner',ID,'--retained-root',str(tomb),'--preserve-python-caches'],capture_output=True,text=True,timeout=30)
        self.assertEqual(done.returncode,0,done.stdout+done.stderr)
        self.assertEqual(json.loads(done.stdout)['status'],'proven')

    def test_cache_verification_does_not_execute_the_tracked_source(self):
        sentinel=self.base/'source-executed'
        (self.source/'pkg/module.py').write_text('from pathlib import Path\nPath('+repr(str(sentinel))+').touch()\n')
        self.git(self.source,'add','pkg/module.py');self.git(self.source,'commit','-qm','fixture source must not execute')
        self.sha=self.git(self.source,'rev-parse','HEAD')
        self.git(self.root,'fetch','-q','origin','main');self.git(self.root,'checkout','-q','--detach',self.sha)
        (self.record/'base-sha').write_text(self.sha+'\n');self.cache();self.proof(0,preserve=True)
        self.assertFalse(sentinel.exists())

    def test_nonreproduced_cache_requires_complete_recoverable_preservation(self):
        cache=self.cache();original=cache.read_bytes()+b'original extra bytes must survive'
        cache.write_bytes(original);self.proof(2)
        archive=self.record/'python-cache-archive';self.assertFalse(archive.exists())
        result=self.proof(0,preserve=True)
        receipt=next(archive.glob('*.json'));metadata=json.loads(receipt.read_text())
        self.assertEqual(metadata['owner'],ID)
        self.assertEqual(metadata['source_root'],str(self.source))
        self.assertEqual(metadata['relative_path'],str(cache.relative_to(self.root)))
        self.assertEqual(metadata['bytes'],len(original))
        self.assertEqual(metadata['sha256'],hashlib.sha256(original).hexdigest())
        saved=receipt.with_suffix('.pyc');self.assertEqual(saved.read_bytes(),original)
        self.assertEqual(archive.stat().st_mode&0o777,0o700)
        self.assertEqual(saved.stat().st_mode&0o777,0o600)
        self.assertEqual(result['python_cache_proofs'][0]['kind'],'preserved_original')
        self.proof(0)
        shutil.rmtree(self.root)
        self.assertEqual(saved.read_bytes(),original)

    def test_unsafe_cache_archive_never_allows_proof(self):
        cache=self.cache();cache.write_bytes(cache.read_bytes()+b'needs preservation')
        outside=self.base/'outside-archive';outside.mkdir()
        archive=self.record/'python-cache-archive';archive.symlink_to(outside,target_is_directory=True)
        self.proof(2,preserve=True);self.assertEqual(list(outside.iterdir()),[])

    def test_corrupt_or_linked_preserved_cache_is_not_trusted(self):
        cache=self.cache();original=cache.read_bytes()+b'needs preservation';cache.write_bytes(original)
        self.proof(0,preserve=True)
        saved=next((self.record/'python-cache-archive').glob('*.pyc'))
        saved.write_bytes(b'partial');self.proof(2)
        saved.write_bytes(original);self.proof(0)
        os.link(saved,self.base/'extra-link');self.proof(2)

    def test_preservation_fsync_failure_keeps_the_root_unproven(self):
        cache=self.cache();cache.write_bytes(cache.read_bytes()+b'needs preservation')
        module=runpy.run_path(str(ROOT/'bin/harness_session_work_proof.py'),run_name='proof_fixture')
        with mock.patch.object(os,'fsync',side_effect=OSError('fixture fsync failure')):
            with self.assertRaises(ValueError):module['prove'](self.state,ID,preserve_python_caches=True)
        self.assertTrue(self.root.is_dir());self.assertTrue(cache.is_file());self.proof(2)

    def test_preservation_metadata_and_private_modes_are_required(self):
        cache=self.cache();cache.write_bytes(cache.read_bytes()+b'needs preservation');self.proof(0,preserve=True)
        archive=self.record/'python-cache-archive';receipt=next(archive.glob('*.json'));original=receipt.read_bytes()
        receipt.write_bytes(b'partial');self.proof(2,preserve=True);self.assertEqual(receipt.read_bytes(),b'partial')
        receipt.write_bytes(original);receipt.chmod(0o644);self.proof(2,preserve=True)
        receipt.chmod(0o600);self.proof(0)
        saved=receipt.with_suffix('.pyc');saved.unlink();saved.symlink_to(cache);self.proof(2)

    def test_preservation_limits_retain_the_original_cache(self):
        cache=self.cache();cache.write_bytes(cache.read_bytes()+b'needs preservation')
        archive=self.record/'python-cache-archive';archive.mkdir(mode=0o700)
        large=archive/('0'*64+'-'+'1'*64+'.pyc')
        with large.open('wb') as handle:handle.truncate(64*1024*1024)
        self.proof(2,preserve=True);self.assertTrue(cache.is_file());large.unlink()
        for number in range(257):
            (archive/(f'{number:064x}-'+'1'*64+'.pyc')).touch()
        self.proof(2,preserve=True);self.assertTrue(cache.is_file())

    def test_preservation_directory_replacement_does_not_prove_the_root(self):
        cache=self.cache();cache.write_bytes(cache.read_bytes()+b'needs preservation')
        module=runpy.run_path(str(ROOT/'bin/harness_session_work_proof.py'),run_name='proof_fixture')
        archive=self.record/'python-cache-archive';real_fsync=os.fsync;swapped=[]
        def replace_after_write(fd):
            real_fsync(fd)
            if not swapped and archive.is_dir():
                archive.rename(self.base/'detached-archive');archive.mkdir(mode=0o700);swapped.append(True)
        with mock.patch.object(os,'fsync',side_effect=replace_after_write):
            with self.assertRaises(ValueError):module['prove'](self.state,ID,preserve_python_caches=True)
        self.assertTrue(self.root.is_dir());self.proof(2)

    def test_bytecode_never_hides_changed_or_staged_source(self):
        self.cache();source=self.root/'pkg/module.py'
        source.write_text('VALUE = 2\n');self.cache();self.proof(2,preserve=True)
        self.git(self.root,'add','pkg/module.py');source.write_text('VALUE = 1\n');self.proof(2,preserve=True)

    def test_cache_directory_notes_or_orphan_bytecode_remain_unaccounted(self):
        cache=self.cache();notes=cache.parent/'notes.txt';notes.write_text('unique notes\n');self.proof(2,preserve=True)
        notes.unlink()
        orphan=cache.parent/('orphan.'+sys.implementation.cache_tag+'.pyc')
        shutil.copyfile(cache,orphan);self.proof(2,preserve=True)

    def test_cache_symlinks_and_executable_payloads_are_retained(self):
        cache=self.cache();outside=self.base/'outside';outside.write_text('unique data\n')
        cache.unlink();cache.symlink_to(outside);self.proof(2)
        self.assertEqual(outside.read_text(),'unique data\n')
        cache.unlink();cache=self.cache();cache.chmod(0o755);self.proof(2)

    def test_unversioned_or_foreign_tag_bytecode_remains_unaccounted(self):
        cache=self.cache()
        for name in ('module.pyc','module.foreign-312.pyc','module.cpython-312.opt-9.pyc'):
            with self.subTest(name=name):
                extra=cache.parent/name;shutil.copyfile(cache,extra);self.proof(2);extra.unlink()

    def test_staged_bytecode_is_not_discarded_as_an_untracked_cache(self):
        cache=self.cache();self.git(self.root,'add','-f',str(cache.relative_to(self.root)));self.proof(2,preserve=True)
        self.assertFalse((self.record/'python-cache-archive').exists())

    def test_tracked_bytecode_remains_subject_to_content_proof(self):
        source_cache=Path(py_compile.compile(str(self.source/'pkg/module.py'),doraise=True))
        relative=str(source_cache.relative_to(self.source))
        self.git(self.source,'add','-f',relative);self.git(self.source,'commit','-qm','tracked fixture bytecode')
        self.sha=self.git(self.source,'rev-parse','HEAD')
        self.git(self.root,'fetch','-q','origin','main');self.git(self.root,'checkout','-q','--detach',self.sha)
        (self.record/'base-sha').write_text(self.sha+'\n');self.proof(0)
        (self.root/relative).write_bytes(b'changed tracked cache\n');self.proof(2)

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
