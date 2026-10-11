#!/usr/bin/env python3
"""Regression tests for launcher-owned native transcript archives."""
import hashlib
import os
import json
import sys
import tempfile
import unittest
import shutil
import subprocess
from unittest import mock
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))


def native_restore_fixture(source, state, home, receipt, native):
    """Exercise the real Native snapshot/restore metadata contract on owned data."""
    source, state = source.resolve(), state.resolve()
    original = source / '.harness/codex'
    shutil.copytree(home, original)
    helper = Path(__file__).resolve().parents[1] / 'bin/codex-history.py'
    result = subprocess.run([sys.executable, '-I', str(helper), 'snapshot', '--source', str(original),
        '--catalog', str(state / 'native-history'), '--receipt', str(state / 'captured.json'),
        '--source-root', str(source), '--isolation-id', 'canonical', '--runtime-revision', 'a'*40],
        capture_output=True, text=True, check=True)
    snapshot = json.loads(result.stdout)['snapshot_id']
    shutil.rmtree(home)
    subprocess.run([sys.executable, '-I', str(helper), 'restore', '--catalog', str(state / 'native-history'),
        '--snapshot', snapshot, '--destination', str(home), '--receipt', str(receipt),
        '--native-id', native, '--origin-runtime', 'a'*40], capture_output=True, text=True, check=True)
    return snapshot


class SessionArchiveTest(unittest.TestCase):
    def test_existing_snapshot_cannot_prove_changed_live_history(self):
        from harness_session_archive import archive,ArchiveError
        owner='11111111-1111-4111-8111-111111111111'
        with tempfile.TemporaryDirectory() as tmp:
            root,state,source=Path(tmp,'root'),Path(tmp,'state'),Path(tmp,'source');source.mkdir()
            raw=root/'.harness/codex/sessions/file.jsonl';raw.parent.mkdir(parents=True);raw.write_bytes(b'original\n')
            saved=archive(root=root,state=state,owner_id=owner,source_root=source)
            before=(saved/'manifest.json').read_bytes()
            raw.write_bytes(b'original\nnew turn\n')
            with self.assertRaises(ArchiveError):archive(root=root,state=state,owner_id=owner,source_root=source)
            self.assertEqual(raw.read_bytes(),b'original\nnew turn\n')
            self.assertEqual((saved/'manifest.json').read_bytes(),before)
            raw.write_bytes(b'original\n')
            self.assertEqual(archive(root=root,state=state,owner_id=owner,source_root=source),saved)

    def test_files_added_or_changed_after_copy_prevent_publication(self):
        import harness_session_archive as module
        for added in (True,False):
            with self.subTest(added=added),tempfile.TemporaryDirectory() as tmp:
                root,state,source=Path(tmp,'root'),Path(tmp,'state'),Path(tmp,'source');source.mkdir()
                folder=root/'.harness/codex/sessions';folder.mkdir(parents=True)
                first=folder/'a.jsonl';first.write_bytes(b'original\n');(folder/'b.jsonl').write_bytes(b'other\n')
                original_copy=module._copy_file
                def copy(incoming,outgoing,total):
                    result=original_copy(incoming,outgoing,total)
                    if incoming.name=='b.jsonl':
                        if added:(folder/'c.jsonl').write_bytes(b'new turn\n')
                        else:first.write_bytes(b'changed!\n')
                    return result
                owner='11111111-1111-4111-8111-111111111111'
                with mock.patch.object(module,'_copy_file',side_effect=copy),self.assertRaises(module.ArchiveError):
                    module.archive(root=root,state=state,owner_id=owner,source_root=source)
                self.assertFalse((state/'archives'/owner).exists())

    def test_headless_child_keeps_archive_retention_policy(self):
        from harness_headless import child_env

        prior = os.environ.get('HARNESS_SESSION_RETENTION_SECONDS')
        self.addCleanup(lambda: os.environ.__setitem__('HARNESS_SESSION_RETENTION_SECONDS', prior)
                        if prior is not None else os.environ.pop('HARNESS_SESSION_RETENTION_SECONDS', None))
        os.environ['HARNESS_SESSION_RETENTION_SECONDS'] = 'archive'
        self.assertEqual(child_env('/tmp/run')['HARNESS_SESSION_RETENTION_SECONDS'], 'archive')

    def test_archives_native_codex_transcript_with_exact_manifest(self):
        from harness_session_archive import archive, verify

        owner = '11111111-1111-4111-8111-111111111111'
        native = '22222222-2222-4222-8222-222222222222'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp, 'worktree')
            source = Path(tmp, 'source')
            state = Path(tmp, 'state')
            transcript = root / ' .harness'.strip() / 'codex' / 'sessions' / '2026' / '10' / f'{native}.jsonl'
            transcript.parent.mkdir(parents=True)
            data = b'{"type":"session_meta","payload":{"id":"' + native.encode() + b'"}}\n'
            transcript.write_bytes(data)
            index = b'synthetic native index'
            (root / '.harness' / 'codex' / 'state-v2.sqlite').write_bytes(index)
            (root / '.harness' / 'codex' / 'auth.json').write_text('must not archive')
            source.mkdir()

            published = archive(root=root, state=state, owner_id=owner, source_root=source)

            self.assertEqual(published, state / 'archives' / owner)
            self.assertEqual((published / 'codex' / 'sessions' / '2026' / '10' / f'{native}.jsonl').read_bytes(), data)
            self.assertFalse((published / 'codex' / 'auth.json').exists())
            manifest = verify(archive=published, owner_id=owner, source_root=source)
            self.assertEqual(manifest['session_id'], owner)
            self.assertEqual(manifest['grant_provenance_sha256'], hashlib.sha256(
                json.dumps(manifest['grant_provenance'], sort_keys=True, separators=(',', ':')).encode()).hexdigest())
            self.assertEqual(manifest['files'], [{
                'path': f'codex/sessions/2026/10/{native}.jsonl',
                'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
            }, {
                'path': 'codex/state-v2.sqlite',
                'bytes': len(index), 'sha256': hashlib.sha256(index).hexdigest(),
            }])

    def test_archive_grant_provenance_does_not_follow_later_global_record_changes(self):
        from harness_session_archive import archive, verify

        owner = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
        native = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
        with tempfile.TemporaryDirectory() as tmp:
            root, state, source = Path(tmp, 'root'), Path(tmp, 'state'), Path(tmp, 'source')
            source.mkdir(); raw = root / '.harness' / 'codex' / 'sessions' / f'{native}.jsonl'; raw.parent.mkdir(parents=True)
            raw.write_text('{"type":"session_meta","payload":{"id":"%s"}}\n' % native)
            records = state / 'launch-records'; records.mkdir(parents=True)
            record = records / ('codex-' + native)
            record.write_text('profile=base\nsandbox=workspace-write\nnetwork=enabled\nsource_root=%s\nisolated=1\nharness_session_id=%s\n' % (source.resolve(), owner))
            archive(root=root, state=state, owner_id=owner, source_root=source)
            record.write_text('profile=astra\nsource_root=%s\nisolated=1\nharness_session_id=%s\n' % (source.resolve(), owner))

            manifest = verify(archive=state / 'archives' / owner, owner_id=owner, source_root=source)
            self.assertEqual(manifest['grant_provenance']['codex'][0]['profile'], 'base')
            self.assertEqual(manifest['grant_provenance']['codex'][0]['network'], 'enabled')

    def test_archive_persists_valid_native_restore_selection_and_reuses_only_matching_selection(self):
        from harness_session_archive import archive, verify, ArchiveError
        owner='11111111-1111-4111-8111-111111111111';native='22222222-2222-4222-8222-222222222222';snapshot='a'*64
        with tempfile.TemporaryDirectory() as tmp:
            root,state,source=Path(tmp,'root'),Path(tmp,'state'),Path(tmp,'source');source.mkdir()
            raw=root/'.harness/codex/sessions'/(native+'.jsonl');raw.parent.mkdir(parents=True)
            raw.write_text(json.dumps({'type':'session_meta','payload':{'id':native}})+'\n'+json.dumps({'type':'turn_context','payload':{}})+'\n')
            record=state/'sessions'/owner;record.mkdir(parents=True)
            receipt=record/'native-history-restore.json'
            snapshot=native_restore_fixture(source,state,root/'.harness/codex',receipt,native)
            saved=archive(root=root,state=state,owner_id=owner,source_root=source)
            manifest=verify(archive=saved,owner_id=owner,source_root=source)
            self.assertEqual(manifest['native_restore_selection'],{'native_id':native,'snapshot_id':snapshot})
            self.assertEqual((saved/'codex/sessions'/(native+'.jsonl')).read_bytes(),raw.read_bytes())
            self.assertEqual(archive(root=root,state=state,owner_id=owner,source_root=source),saved)
            value=json.loads(receipt.read_text());value['native_id']='33333333-3333-4333-8333-333333333333'
            receipt.write_text(json.dumps(value)+'\n')
            with self.assertRaises(ArchiveError):archive(root=root,state=state,owner_id=owner,source_root=source)

    def test_native_restore_metadata_invalid_before_body_and_race_blocks_publication(self):
        import harness_session_archive as module
        owner='11111111-1111-4111-8111-111111111111';native='22222222-2222-4222-8222-222222222222'
        cases=('mode','duplicate','source','hash','receipt-shape','receipt-link','manifest-link','race')
        for case in cases:
            with self.subTest(case=case),tempfile.TemporaryDirectory() as tmp:
                root,state,source=Path(tmp,'root'),Path(tmp,'state'),Path(tmp,'source');source.mkdir()
                raw=root/'.harness/codex/sessions'/(native+'.jsonl');raw.parent.mkdir(parents=True)
                raw.write_text(json.dumps({'type':'session_meta','payload':{'id':native}})+'\n')
                record=state/'sessions'/owner;record.mkdir(parents=True);receipt=record/'native-history-restore.json'
                snapshot=native_restore_fixture(source,state,root/'.harness/codex',receipt,native)
                original=raw.read_bytes();manifest=state/'native-history/snapshots'/snapshot/'manifest.json'
                if case=='mode':receipt.chmod(0o644)
                elif case=='duplicate':receipt.write_text(receipt.read_text().replace('{','{"native_id":"'+native+'",',1))
                elif case in ('source','hash'):
                    value=json.loads(manifest.read_text());value['source_root']='foreign' if case=='source' else str(source.resolve());value['unexpected']='changed'
                    manifest.write_text(json.dumps(value))
                elif case=='receipt-shape':receipt.write_text('{"snapshot_id":"'+snapshot+'"}')
                elif case in ('receipt-link','manifest-link'):
                    target=receipt if case=='receipt-link' else manifest
                    outside=Path(tmp,'outside');target.rename(outside);target.symlink_to(outside)
                if case=='race':
                    original_copy=module._copy_file
                    def changed(*args):
                        result=original_copy(*args);receipt.write_text(receipt.read_text()+'\n');return result
                    with mock.patch.object(module,'_copy_file',side_effect=changed),self.assertRaises(module.ArchiveError):
                        module.archive(root=root,state=state,owner_id=owner,source_root=source)
                else:
                    with mock.patch.object(module,'_candidate_files',side_effect=AssertionError('body scanned before receipt proof')),self.assertRaises(module.ArchiveError):
                        module.archive(root=root,state=state,owner_id=owner,source_root=source)
                self.assertEqual(raw.read_bytes(),original);self.assertFalse((state/'archives'/owner).exists())

    def test_preexisting_archive_without_selection_cannot_be_reused_after_native_restore(self):
        import harness_session_archive as module
        owner='11111111-1111-4111-8111-111111111111';native='22222222-2222-4222-8222-222222222222'
        with tempfile.TemporaryDirectory() as tmp:
            root,state,source=Path(tmp,'root'),Path(tmp,'state'),Path(tmp,'source');source.mkdir()
            raw=root/'.harness/codex/sessions'/(native+'.jsonl');raw.parent.mkdir(parents=True)
            raw.write_text(json.dumps({'type':'session_meta','payload':{'id':native}})+'\n')
            saved=module.archive(root=root,state=state,owner_id=owner,source_root=source);before=(saved/'manifest.json').read_bytes()
            record=state/'sessions'/owner;record.mkdir(parents=True)
            native_restore_fixture(source,state,root/'.harness/codex',record/'native-history-restore.json',native)
            with self.assertRaises(module.ArchiveError):module.archive(root=root,state=state,owner_id=owner,source_root=source)
            self.assertEqual((saved/'manifest.json').read_bytes(),before)


class ArchiveSafetyTest(unittest.TestCase):
    def setUp(self):
        import harness_session_archive as archive_module
        self.module=archive_module;self.temp=tempfile.TemporaryDirectory(prefix='session-archive-safety-')
        self.addCleanup(self.temp.cleanup);self.base=Path(self.temp.name).resolve()
        self.root,self.source,self.state=(self.base/name for name in ('worktree','source','state'))
        for path in (self.root,self.source,self.state):path.mkdir(mode=0o700)
        self.owner='11111111-1111-4111-8111-111111111111';self.native='22222222-2222-4222-8222-222222222222'
        self.raw=self.root/'.harness/codex/sessions/test'/(self.native+'.jsonl');self.raw.parent.mkdir(parents=True)
        self.raw.write_text(json.dumps({'type':'session_meta','payload':{'id':self.native}})+'\n');self.raw.chmod(0o600)

    def archive(self):
        return self.module.archive(root=self.root,state=self.state,owner_id=self.owner,source_root=self.source)

    def verify(self, target):
        return self.module.verify(archive=target,owner_id=self.owner,source_root=self.source)

    def mutate(self, target, change):
        manifest=json.loads((target/'manifest.json').read_text());change(manifest)
        (target/'manifest.json').write_text(json.dumps(manifest))

    def test_manifest_rejects_non_object_invalid_identity_and_oversized_json(self):
        target=self.archive();original=(target/'manifest.json').read_bytes()
        for value in (None,[],{}, {'version':True,'session_id':self.owner,'source_root':str(self.source)}):
            with self.subTest(value=value):
                (target/'manifest.json').write_text(json.dumps(value))
                with self.assertRaises(self.module.ArchiveError):self.verify(target)
        (target/'manifest.json').write_bytes(original)
        with patch.object(self.module,'MAX_MANIFEST_BYTES',100,create=True):
            with self.assertRaises(self.module.ArchiveError):self.verify(target)

    def test_manifest_rejects_non_native_path_duplicate_and_invalid_entry_types(self):
        target=self.archive();original=(target/'manifest.json').read_bytes()
        extra=target/'codex/auth.json';extra.write_bytes(b'x');extra.chmod(0o600)
        self.mutate(target,lambda value:value['files'].append({'path':'codex/auth.json','bytes':1,'sha256':hashlib.sha256(b'x').hexdigest()}))
        with self.assertRaises(self.module.ArchiveError):self.verify(target)
        extra.unlink();(target/'manifest.json').write_bytes(original)
        self.mutate(target,lambda value:value['files'].append(dict(value['files'][0])))
        with self.assertRaises(self.module.ArchiveError):self.verify(target)
        for bad in (True,-1,'1',None):
            (target/'manifest.json').write_bytes(original)
            self.mutate(target,lambda value:value['files'][0].update(bytes=bad))
            with self.assertRaises(self.module.ArchiveError):self.verify(target)

    def test_manifest_boolean_bytes_cannot_match_one_byte_native_index(self):
        index=self.root/'.harness/codex/state-v2.sqlite';index.write_bytes(b'x')
        target=self.archive()
        self.mutate(target,lambda value:next(item for item in value['files'] if item['path'].endswith('.sqlite')).update(bytes=True))
        with self.assertRaises(self.module.ArchiveError):self.verify(target)

    def test_manifest_rejects_invalid_grant_and_import_shapes(self):
        target=self.archive();original=(target/'manifest.json').read_bytes()
        for grant in ({'codex':[{'native_id':123}]},{'foreign':[]},{'codex':'invalid'}):
            (target/'manifest.json').write_bytes(original)
            def change(value):
                value['grant_provenance']=grant;value['grant_provenance_sha256']=hashlib.sha256(json.dumps(grant,sort_keys=True,separators=(',',':')).encode()).hexdigest()
            self.mutate(target,change)
            with self.assertRaises(self.module.ArchiveError):self.verify(target)
        (target/'manifest.json').write_bytes(original)
        self.mutate(target,lambda value:value.update(restored_native_imports=[{'runtime':'codex','native_id':[], 'source_owner':self.owner}]))
        with self.assertRaises(self.module.ArchiveError):self.verify(target)

    def test_candidate_rejects_intermediate_symlink_and_group_writable_source(self):
        codex=self.root/'.harness/codex';outside=self.base/'outside-codex';codex.rename(outside);codex.symlink_to(outside,target_is_directory=True)
        with self.assertRaises(self.module.ArchiveError):self.archive()
        codex.unlink();outside.rename(codex);self.raw.chmod(0o660)
        with self.assertRaises(self.module.ArchiveError):self.archive()

    def test_candidate_rejects_hardlink_fifo_and_wrong_uid(self):
        other=self.raw.with_name('linked.jsonl');os.link(self.raw,other)
        with self.assertRaises(self.module.ArchiveError):self.archive()
        other.unlink()
        if hasattr(os,'mkfifo'):
            os.mkfifo(other)
            with self.assertRaises(self.module.ArchiveError):self.archive()
            other.unlink()
        with patch.object(self.module.os,'getuid',return_value=os.getuid()+1):
            with self.assertRaises(self.module.ArchiveError):self.module._regular(self.raw)

    def test_verify_rejects_unlisted_fifo_and_group_writable_archive(self):
        target=self.archive();raw=target/'codex/sessions/test'/(self.native+'.jsonl')
        if hasattr(os,'mkfifo'):
            fifo=raw.with_name('unlisted.jsonl');os.mkfifo(fifo)
            with self.assertRaises(self.module.ArchiveError):self.verify(target)
            fifo.unlink()
        raw.chmod(0o660)
        with self.assertRaises(self.module.ArchiveError):self.verify(target)

    def test_bounded_file_total_file_count_depth_and_node_scans(self):
        for key,limit in (('MAX_FILE_BYTES',10),('MAX_TOTAL_BYTES',10),('MAX_FILES',0),('MAX_DEPTH',2),('MAX_NODES',1)):
            with self.subTest(budget=key),patch.object(self.module,key,limit,create=True):
                with self.assertRaises(self.module.ArchiveError):self.archive()

    def test_invalid_import_ledger_is_not_silently_dropped(self):
        record=self.state/'sessions'/self.owner;record.mkdir(parents=True)
        ledger=record/'restored-native-sessions'
        for data in ('not-json','[]',json.dumps({'schema':1,'session_id':self.owner,'source_root':str(self.source),'imports':[{'runtime':'codex','native_id':[],'source_owner':self.owner}]})):
            ledger.write_text(data);ledger.chmod(0o600)
            with self.assertRaises(self.module.ArchiveError):self.archive()

    def test_verify_streams_files_instead_of_read_bytes(self):
        target=self.archive()
        with patch.object(Path,'read_bytes',side_effect=AssertionError('unbounded file read')):
            self.assertEqual(self.verify(target)['session_id'],self.owner)

    def test_same_size_mtime_restored_source_change_is_detected_during_copy(self):
        original_stat=self.raw.stat(); original=self.module._regular;calls=0
        def changed(path):
            nonlocal calls
            if Path(path)==self.raw:
                calls+=1
                if calls==3:
                    value=self.raw.read_bytes();self.raw.write_bytes(value.replace(b'2222',b'3333',1));os.utime(self.raw,ns=(original_stat.st_atime_ns,original_stat.st_mtime_ns))
            return original(path)
        with patch.object(self.module,'_regular',side_effect=changed):
            with self.assertRaises(self.module.ArchiveError):self.archive()

    def test_native_archive_directories_and_files_are_private(self):
        target=self.archive()
        for path in (target,*target.rglob('*')):
            self.assertEqual(path.stat().st_mode&0o777,0o700 if path.is_dir() else 0o600)

    def test_native_metadata_prefix_never_follows_links_or_reads_fifo(self):
        linked=self.raw.with_name('linked.jsonl');linked.symlink_to(self.raw)
        self.assertIsNone(self.module._native_id(linked));linked.unlink()
        if hasattr(os,'mkfifo'):
            os.mkfifo(linked);self.assertIsNone(self.module._native_id(linked))


if __name__ == '__main__':
    unittest.main()
