"""Explicit Claude forks preserve parents and hold both leases through exec."""
import fcntl
import importlib
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import unittest

BIN = Path(__file__).resolve().parents[1] / 'bin'
sys.path.insert(0, str(BIN))
NATIVE = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
OWNER = '11111111-2222-4333-8444-555555555555'
CHILD = '99999999-2222-4333-8444-555555555555'

class ClaudeForkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.source = self.base / 'source'; self.source.mkdir()
        self.state = self.base / 'state'; self.home = self.base / 'home'
        self.record = self.state / 'sessions' / OWNER; self.record.mkdir(parents=True)
        self.root = self.state / 'worktrees' / OWNER
        for name, text in {'source-root': str(self.source), 'session-root': str(self.root), 'journal': 'state=DELIVERED\n', 'provider-sessions': 'claude ' + NATIVE + '\n', 'runtime.lock': ''}.items():
            (self.record / name).write_text(text)
        self.path = self.home / '.claude/projects/project' / (NATIVE + '.jsonl')
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({'sessionId': NATIVE, 'cwd': str(self.root), 'message': 'fixture'}) + '\n')

    def module(self):
        self.assertTrue((BIN / 'harness_claude_fork.py').exists(), 'explicit-fork verifier must exist')
        mod = importlib.import_module('harness_claude_fork')
        if not getattr(self, '_top_level_patched', False):
            # hermetic default: results never depend on a TTY; inheritance tests (prepare_as) patch explicitly
            self._top_level_patched = True
            original = mod._context.launch_top_level
            mod._context.launch_top_level = lambda *a, **k: (False, 'test')
            self.addCleanup(setattr, mod._context, 'launch_top_level', original)
        return mod

    def prepare(self):
        prepared = self.module().prepare(self.source, self.state, NATIVE, self.home)
        self.addCleanup(prepared.close)
        return prepared

    def contender(self, path):
        return subprocess.run([sys.executable, '-c', 'import fcntl,os,sys; f=os.open(sys.argv[1],os.O_RDWR); fcntl.lockf(f,fcntl.LOCK_EX|fcntl.LOCK_NB)', str(path)], capture_output=True).returncode

    def test_terminal_root_can_be_absent_parent_bytes_unchanged_and_recheck_keeps_locks(self):
        before = {p.name: p.read_bytes() for p in self.record.iterdir()}
        transcript = self.path.read_bytes()
        prepared = self.prepare()
        self.assertEqual(prepared.receipt['locator'], str(self.path))
        self.assertEqual(prepared.receipt['source_owner'], OWNER)
        self.assertEqual(prepared.receipt['source_status'], 'DELIVERED')
        self.assertTrue(all(fd >= 10 and os.get_inheritable(fd) for fd in prepared.fds))
        prepared.recheck()
        for path in (self.record/'runtime.lock', self.state/'native-history/leases'/('claude-'+NATIVE+'.lock')):
            self.assertNotEqual(self.contender(path), 0)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.record.iterdir()})
        self.assertEqual(transcript, self.path.read_bytes())
        prepared.close(); prepared.close()
        self.assertEqual(self.contender(self.record/'runtime.lock'), 0)

    def test_recheck_rejects_parent_mutation(self):
        prepared = self.prepare()
        self.path.write_text(self.path.read_text() + '{}\n')
        with self.assertRaises(ValueError): prepared.recheck()
        self.assertEqual(self.contender(self.record/'runtime.lock'),0)
        self.assertEqual(prepared.fds,())

    def test_rejected_states_release_native_lease(self):
        for status in ('OPEN', 'SUBMITTED', 'INTEGRATING', 'ABANDONED', 'CONFLICT', 'UNKNOWN'):
            with self.subTest(status=status):
                (self.record/'journal').write_text('state='+status+'\n')
                with self.assertRaises(ValueError): self.prepare()
                lease = self.state/'native-history/leases'/('claude-'+NATIVE+'.lock')
                self.assertEqual(self.contender(lease), 0)

    def test_symlink_ancestor_hardlink_and_writable_transcript_refused(self):
        mod = self.module()
        self.path.chmod(0o666)
        with self.assertRaises(ValueError): self.prepare()
        self.path.chmod(0o600)
        linked = self.base/'linked'; os.link(self.path, linked)
        with self.assertRaises(ValueError): self.prepare()
        linked.unlink()
        real = self.home/'.claude/projects/real'
        self.path.parent.rename(real)
        self.path.parent.symlink_to(real, target_is_directory=True)
        with self.assertRaises(ValueError): self.prepare()

    def test_legacy_has_no_proof_of_inactivity(self):
        (self.record/'provider-sessions').unlink()
        self.path.write_text(json.dumps({'sessionId': NATIVE, 'cwd': str(self.source)})+'\n')
        with self.assertRaisesRegex(ValueError, 'inactivity|owner'): self.prepare()

    def test_exec_inherits_locks_until_native_process_exit(self):
        self.module()
        script = '''import os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from harness_claude_fork import prepare
p=prepare(Path(sys.argv[2]),Path(sys.argv[3]),sys.argv[4],Path(sys.argv[5]))
os.execv(sys.executable,[sys.executable,'-c','import sys; print("ready",flush=True); sys.stdin.readline()'])
'''
        child = subprocess.Popen([sys.executable,'-c',script,str(BIN),str(self.source),str(self.state),NATIVE,str(self.home)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: child.poll() is None and child.kill())
        self.assertEqual(child.stdout.readline().strip(), 'ready')
        self.assertNotEqual(self.contender(self.record/'runtime.lock'), 0)
        with self.assertRaises(ValueError): self.prepare()
        child.communicate('\n', timeout=5)
        self.assertEqual(child.returncode, 0)
        self.assertEqual(self.contender(self.record/'runtime.lock'), 0)

    def test_runtime_contender_prevents_prepare_and_releases_native_lease(self):
        self.module()
        script='import fcntl,sys; f=open(sys.argv[1],"r+"); fcntl.lockf(f,fcntl.LOCK_EX); print("ready",flush=True); sys.stdin.readline()'
        child=subprocess.Popen([sys.executable,'-c',script,str(self.record/'runtime.lock')],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
        self.assertEqual(child.stdout.readline().strip(),'ready')
        try:
            with self.assertRaises(ValueError): self.prepare()
            self.assertEqual(self.contender(self.state/'native-history/leases'/('claude-'+NATIVE+'.lock')),0)
        finally: child.communicate('\n',timeout=5)

    def test_request_distinguishes_values_prompt_and_native_controls(self):
        mod = self.module()
        self.assertTrue(hasattr(mod, 'request'), 'explicit fork request parser required')
        for args in (['--resume',NATIVE,'--fork-session'], ['rich','bypass','--passthrough','--fork-session','--resume='+NATIVE], ['resume',NATIVE,'--fork-session'], ['-r',NATIVE], ['--print','prompt','--fork-session','--resume',NATIVE]):
            with self.subTest(args=args):
                result = mod.request(args)
                self.assertEqual(result['native_id'],NATIVE)
                self.assertEqual(result['fork'],'--fork-session' in args)
        for args in (['codex','resume',NATIVE], ['kiro','--resume',NATIVE,'--fork-session'], ['--model','--resume='+NATIVE], ['--','--resume',NATIVE,'--fork-session'], ['--settings','--fork-session']):
            with self.subTest(args=args): self.assertIsNone(mod.request(args))
        for args in (['--fork-session'], ['--resume','bad','--fork-session'], ['--resume',NATIVE,'-r',NATIVE], ['--unknown','value','--resume',NATIVE,'--fork-session'], ['--resume',NATIVE,'--fork-session','--continue']):
            with self.subTest(args=args), self.assertRaises(ValueError): mod.request(args)

    def test_rewrite_normalizes_shorthand_and_inserts_safe_permission_before_prompt(self):
        mod=self.module()
        self.assertTrue(hasattr(mod,'rewrite'),'verified fork argv rewrite required')
        prepared=self.prepare()
        args=['rich','resume',NATIVE,'--fork-session','--','--permission-mode','bypassPermissions']
        rewritten=mod.rewrite(args,prepared.receipt)
        self.assertEqual(rewritten,['rich','--passthrough','--resume',str(self.path),'--fork-session','--permission-mode','default','--','--permission-mode','bypassPermissions'])
        explicit=['base','bypass','--passthrough','--resume='+NATIVE,'--fork-session']
        self.assertEqual(mod.rewrite(explicit,prepared.receipt),['base','bypass','--passthrough','--resume',str(self.path),'--fork-session'])
        value=['--model','--permission-mode','--resume',NATIVE,'--fork-session']
        self.assertEqual(mod.rewrite(value,prepared.receipt)[-2:],['--permission-mode','default'])

    def test_rewritten_fork_reaches_real_aliases_native_without_tui_or_keyword_capture(self):
        module=self.module()
        prepared=self.prepare()
        (self.source/'config').mkdir()
        (self.source/'config/launcher.env').write_text('HARNESS_NAME="Fixture"\nHARNESS_PREFIX="fixture"\n')
        (self.source/'.mcp.json').write_text('{"mcpServers":{}}')
        fake_bin=self.base/'native-bin'; fake_bin.mkdir()
        fake_native=fake_bin/'claude'
        fake_native.write_text('#!/usr/bin/env python3\nimport json,os,sys\nwith open(os.environ["FORK_NATIVE_ARGS"],"w") as output: json.dump(sys.argv[1:],output)\n')
        fake_native.chmod(0o755)
        output=self.base/'native-args.json'
        environment=dict(os.environ, HOME=str(self.home), PATH=str(fake_bin)+os.pathsep+os.environ['PATH'],
            FORK_NATIVE_ARGS=str(output), HARNESS_SESSION_STATE_HOME=str(self.state),
            HARNESS_SESSION_ID='', HARNESS_SOURCE_ROOT='', HARNESS_SESSION_ROOT='',
            HARNESS_RUN_DIR='', HARNESS_SESSION_ISOLATION='0', HARNESS_SESSION_ISOLATION_DEFAULT='0',
            HARNESS_CLAUDE_PLUGIN_MIRROR='0')
        cases=[
            (['--resume',NATIVE,'--fork-session'], 'default', None),
            (['rich','bypass','--resume',NATIVE,'--fork-session'], 'bypassPermissions', 'opus[1m]'),
            (['rich','--passthrough','--resume',NATIVE,'--fork-session','--permission-mode','plan','--model','base'], 'plan', 'base'),
            (['resume',NATIVE,'--fork-session','--permission-mode','plan'], 'plan', None),
            (['rich','--resume',NATIVE,'bypass','--fork-session','--permission-mode','acceptEdits','--model','sonnet'], 'acceptEdits', 'sonnet'),
        ]
        for args, permission, model in cases:
            with self.subTest(args=args):
                if output.exists(): output.unlink()
                rewritten=module.rewrite(args,prepared.receipt)
                launched=subprocess.run(['zsh','-c','source "$1"; shift; _harness_launcher_run "$@"','fixture',str(BIN/'aliases.zsh'),str(self.source),*rewritten],env=environment,stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=20)
                self.assertEqual(launched.returncode,0,launched.stderr)
                self.assertTrue(output.exists(),'native Claude must start without another TUI: '+launched.stdout[-800:])
                native=json.loads(output.read_text())
                self.assertEqual(native[native.index('--resume')+1],str(self.path))
                self.assertIn('--fork-session',native)
                self.assertEqual(native.count('--permission-mode'),1,native)
                self.assertEqual(native[native.index('--permission-mode')+1],permission)
                if model: self.assertEqual(native[native.index('--model')+1],model)

    def test_selected_generation_does_not_reuse_cached_catalog_or_archive(self):
        self.module()
        script = """import importlib.util,sys,types
from pathlib import Path
for name in ('harness_session_catalog','harness_session_archive'):
    sys.modules[name]=types.ModuleType(name)
spec=importlib.util.spec_from_file_location('selected_generation',Path(sys.argv[1])/'harness_claude_fork.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
p=module.prepare(Path(sys.argv[2]),Path(sys.argv[3]),sys.argv[4],Path(sys.argv[5]))
print(p.receipt['native_id']); p.close()
"""
        result=subprocess.run([sys.executable,'-c',script,str(BIN),str(self.source),str(self.state),NATIVE,str(self.home)],text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(result.stdout.strip(),NATIVE)

    def test_same_process_double_prepare_must_not_drop_existing_locks(self):
        prepared=self.prepare()
        with self.assertRaises(ValueError): self.prepare()
        self.assertNotEqual(self.contender(self.record/'runtime.lock'),0)
        self.assertNotEqual(self.contender(self.state/'native-history/leases'/('claude-'+NATIVE+'.lock')),0)
        prepared.recheck()

    def test_unsafe_existing_native_lease_and_owner_lock_fail_closed(self):
        module=self.module()
        lease=self.state/'native-history/leases'/('claude-'+NATIVE+'.lock')
        lease.parent.mkdir(parents=True)
        lease.write_text(''); lease.chmod(0o666)
        with self.assertRaises(ValueError): self.prepare()
        lease.chmod(0o600)
        (self.record/'runtime.lock').chmod(0o666)
        with self.assertRaises(ValueError): self.prepare()
        self.assertEqual(self.contender(lease),0)

    def test_recorded_source_cwd_can_supply_terminal_transcript_provenance(self):
        (self.record/'run-dirs').write_text(str(self.source)+'\n')
        self.path.write_text(json.dumps({'sessionId':NATIVE,'cwd':str(self.source)})+'\n')
        prepared=self.prepare()
        self.assertEqual(prepared.receipt['source_owner'],OWNER)
        prepared.recheck()

    def test_malformed_fork_flag_never_falls_back_to_new_session(self):
        module=self.module()
        for args in (['--fork-session=true'], ['--resume',NATIVE,'--fork-session=false'], ['--resume','--fork-session'], ['-r','--fork-session'], ['resume','--fork-session']):
            with self.subTest(args=args), self.assertRaises(ValueError): module.request(args)

    def test_owner_proof_mismatch_or_ambiguous_binding_refused(self):
        self.module()
        for name, value in (('source-root',str(self.base/'other')),('session-root',str(self.source)),('journal','state=CLOSED\nstate=DELIVERED\n'),('provider-sessions','claude '+CHILD+'\n')):
            with self.subTest(name=name):
                path=self.record/name; before=path.read_bytes(); path.write_text(value)
                try:
                    with self.assertRaises(ValueError): self.prepare()
                finally: path.write_bytes(before)
        duplicate=self.state/'sessions'/CHILD; duplicate.mkdir()
        for name in ('source-root','session-root','journal','provider-sessions','runtime.lock'):
            (duplicate/name).write_bytes((self.record/name).read_bytes())
        with self.assertRaises(ValueError): self.prepare()

    def test_bad_cwd_native_metadata_and_renamed_lock_are_refused(self):
        self.module()
        before=self.path.read_bytes()
        for meta in ({'sessionId':CHILD,'cwd':str(self.root)}, {'sessionId':NATIVE,'cwd':str(self.base/'other')}):
            self.path.write_text(json.dumps(meta)+'\n')
            with self.assertRaises(ValueError): self.prepare()
        self.path.write_bytes(before)
        prepared=self.prepare()
        lock=self.record/'runtime.lock'; lock.rename(self.record/'old.lock'); lock.write_text('')
        with self.assertRaises(ValueError): prepared.recheck()

    def test_derived_recorder_rejects_parent_and_accepts_child_and_malformed_fails_closed(self):
        prepared=self.prepare()
        newdir=self.state/'sessions'/CHILD; newdir.mkdir()
        (newdir/'source-root').write_text(str(self.source))
        receipt=dict(prepared.receipt,owner=CHILD)
        origin=newdir/'claude-fork-origin.json'; origin.write_text(json.dumps(receipt)); origin.chmod(0o600)
        env=dict(os.environ,HARNESS_SESSION_STATE_HOME=str(self.state),HARNESS_SESSION_ID=CHILD)
        def record(native):
            result=subprocess.run([sys.executable,str(BIN/'harness-session-provider-record')],input=json.dumps({'hook_event_name':'SessionStart','source':'resume','session_id':native}),text=True,capture_output=True,env=env)
            self.assertEqual((result.returncode,result.stdout,result.stderr),(0,'',''))
        record(NATIVE)
        target=newdir/'provider-sessions'
        self.assertFalse(target.exists())
        record(CHILD)
        self.assertEqual(target.read_text(),'claude '+CHILD+'\n')
        target.unlink(); origin.write_text('{}'); record(CHILD)
        self.assertFalse(target.exists())

    @unittest.skipUnless(sys.platform == 'darwin', 'session broker uses macOS stat identity')
    def test_real_broker_nested_clone_run_directory_can_fork_after_terminal_retirement(self):
        # D's actual resolver must supply a cwd E can later recognize. Removing
        # either catalog or strict-verifier subtree support breaks this flow.
        module=self.module()
        environment=dict(os.environ, HARNESS_SESSION_STATE_HOME=str(self.state),
                         HARNESS_HEADLESS='0', GIT_CONFIG_NOSYSTEM='1',
                         GIT_CONFIG_GLOBAL='/dev/null', HOME=str(self.home))
        def run(argv):
            result=subprocess.run(argv, env=environment, capture_output=True, text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            return result.stdout.strip()
        (self.source/'nested').mkdir()
        (self.source/'nested/file').write_text('fixture')
        run(['git','-C',str(self.source),'init','-q','-b','main'])
        run(['git','-C',str(self.source),'add','nested/file'])
        run(['git','-C',str(self.source),'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','-c','commit.gpgSign=false','commit','-qm','fixture'])
        isolation=str(BIN/'session-isolation.sh')
        created=dict(line.split('=',1) for line in run([isolation,'create',str(self.source)]).splitlines())
        owner=created['HARNESS_SESSION_ID']; root=Path(created['HARNESS_SESSION_ROOT'])
        cwd=run([isolation,'resolve-run-dir',str(self.source),owner,str(self.source/'nested')])
        self.assertEqual(cwd,str(root/'nested'))
        record=self.state/'sessions'/owner
        (record/'run-dirs').write_text(cwd+'\n')
        (record/'provider-sessions').write_text('claude '+NATIVE+'\n')
        (self.record/'provider-sessions').unlink()
        self.path.write_text(json.dumps({'sessionId':NATIVE,'cwd':cwd})+'\n')
        run([isolation,'transition',owner,'CLOSED'])
        parent_journal=(record/'journal').read_bytes()
        parent_transcript=self.path.read_bytes()
        for retired in (False, True):
            with self.subTest(retired=retired):
                if retired: shutil.rmtree(root)
                candidates=module._catalog.catalog(self.state,{'fixture':self.source},self.home)['items']
                self.assertEqual([(item['native_id'],item['launcher_session_id']) for item in candidates],[(NATIVE,owner)])
                prepared=module.prepare(self.source,self.state,NATIVE,self.home)
                try:
                    self.assertEqual(prepared.receipt['source_owner'],owner)
                    self.assertEqual(prepared.receipt['source_status'],'CLOSED')
                    prepared.recheck()
                finally: prepared.close()
                self.assertEqual((record/'journal').read_bytes(),parent_journal)
                self.assertEqual(self.path.read_bytes(),parent_transcript)

    def test_recheck_refuses_recorded_clone_subdirectory_replaced_by_source_symlink(self):
        nested=self.root/'nested'; nested.mkdir(parents=True)
        destination=self.source/'nested'; destination.mkdir()
        (self.record/'run-dirs').write_text(str(nested)+'\n')
        self.path.write_text(json.dumps({'sessionId':NATIVE,'cwd':str(nested)})+'\n')
        prepared=self.prepare()
        nested.rmdir(); nested.symlink_to(destination,target_is_directory=True)
        with self.assertRaises(ValueError): prepared.recheck()
        self.assertEqual(self.contender(self.record/'runtime.lock'),0)

    def test_recorded_run_directory_rejects_prefix_sibling_and_symlink_escape(self):
        module=self.module()
        self.root.mkdir(parents=True)
        other=self.base/'outside'; other.mkdir()
        (self.source/'nested').mkdir()
        (self.root/'outside-link').symlink_to(other,target_is_directory=True)
        (self.root/'source-link').symlink_to(self.source/'nested',target_is_directory=True)
        sibling=self.root.with_name(OWNER+'-sibling'); sibling.mkdir()
        source_sibling=self.source.with_name('source-sibling'); source_sibling.mkdir()
        for cwd in (str(sibling), str(source_sibling), str(self.root/'outside-link'), str(self.root/'source-link'), str(self.root/'..'/sibling.name)):
            with self.subTest(cwd=cwd):
                (self.record/'run-dirs').write_text(cwd+'\n')
                self.path.write_text(json.dumps({'sessionId':NATIVE,'cwd':cwd})+'\n')
                candidates=module._catalog.catalog(self.state,{'fixture':self.source},self.home)['items']
                self.assertEqual(candidates,[])
                with self.assertRaises(ValueError): self.prepare()

    def test_derived_recorder_rejects_unsafe_receipts_and_parent_provider_hardlink(self):
        prepared=self.prepare()
        newdir=self.state/'sessions'/CHILD; newdir.mkdir()
        (newdir/'source-root').write_text(str(self.source))
        origin=newdir/'claude-fork-origin.json'
        receipt=dict(prepared.receipt,owner=CHILD)
        env=dict(os.environ,HARNESS_SESSION_STATE_HOME=str(self.state),HARNESS_SESSION_ID=CHILD)
        event=json.dumps({'hook_event_name':'SessionStart','source':'fork','session_id':CHILD})
        def invoke():
            result=subprocess.run([sys.executable,str(BIN/'harness-session-provider-record')],input=event,text=True,capture_output=True,env=env)
            self.assertEqual((result.returncode,result.stdout,result.stderr),(0,'',''))
        for changes in ({'owner':OWNER},{'source_root':'/other/source'},{'locator_sha256':'bad'},{'bytes':True},{'source_status':'OPEN'},{'runtime':'codex'}):
            with self.subTest(changes=changes):
                origin.write_text(json.dumps(dict(receipt,**changes))); origin.chmod(0o600)
                invoke(); self.assertFalse((newdir/'provider-sessions').exists())
        origin.write_text(json.dumps(receipt)); origin.chmod(0o644)
        invoke(); self.assertFalse((newdir/'provider-sessions').exists())
        origin.chmod(0o600)
        provider=self.record/'provider-sessions'; before=provider.read_bytes()
        os.link(provider,newdir/'provider-sessions')
        invoke(); self.assertEqual(provider.read_bytes(),before)

class ForkInheritanceTests(ClaudeForkTests):
    """Top-level fork with a record bound to the verified origin owner inherits its launch settings."""

    def setUp(self):
        super().setUp()
        self.path.write_text(json.dumps({'sessionId': NATIVE, 'cwd': str(self.root)}) + '\n' + json.dumps(
            {'type': 'assistant', 'effort': 'high', 'message': {'model': 'claude-opus-4-8', 'usage': {'input_tokens': 10}}}) + '\n')
        self.records = self.state / 'launch-records'
        self.records.mkdir(mode=0o700)

    def record_file(self, **changes):
        fields = {'permission': 'bypassPermissions', 'source_root': str(self.source), 'isolated': '1',
                  'harness_session_id': OWNER, 'context': '1m'}
        fields.update(changes)
        path = self.records / ('claude-' + NATIVE)
        if path.is_symlink() or path.exists(): path.unlink()
        path.write_text(''.join('%s=%s\n' % (k, v) for k, v in fields.items() if v is not None))
        path.chmod(0o600)
        return path

    def prepare_as(self, top_level=True):
        mod = self.module()
        mod._context.launch_top_level = lambda *a, **k: (True, None) if top_level else (False, 'agent_ancestor')
        return mod, self.prepare()

    ARGS = ['rich', 'resume', NATIVE, '--fork-session', '--', 'prompt']

    def test_top_level_bound_record_supplies_permission_model_and_effort(self):
        self.record_file()
        mod, prepared = self.prepare_as()
        self.assertEqual(prepared.inherited['permission'], 'bypassPermissions')
        self.assertNotIn('inherited', prepared.receipt)
        self.assertEqual(mod.rewrite(['resume', NATIVE, '--fork-session', '--', 'prompt'], prepared.receipt, prepared.inherited),
                         ['--passthrough', '--resume', str(self.path), '--fork-session', '--permission-mode', 'bypassPermissions',
                          '--model', 'claude-opus-4-8[1m]', '--effort', 'high', '--', 'prompt'])

    def test_without_inherited_rewrite_is_unchanged(self):
        self.record_file()
        mod, prepared = self.prepare_as()
        self.assertEqual(mod.rewrite(['resume', NATIVE, '--fork-session'], prepared.receipt),
                         ['--passthrough', '--resume', str(self.path), '--fork-session', '--permission-mode', 'default'])
        self.assertEqual(mod.rewrite(['resume', NATIVE, '--fork-session'], prepared.receipt, None)[-2:], ['--permission-mode', 'default'])

    def test_not_top_level_is_default(self):
        self.record_file()
        mod, prepared = self.prepare_as(top_level=False)
        self.assertIsNone(prepared.inherited)
        self.assertEqual(mod.rewrite(['resume', NATIVE, '--fork-session'], prepared.receipt, prepared.inherited)[-2:], ['--permission-mode', 'default'])

    def test_missing_record_is_default(self):
        mod, prepared = self.prepare_as()
        self.assertIsNone(prepared.inherited)

    def test_binding_mismatches_are_default(self):
        other = self.base / 'other'; other.mkdir()
        for changes in ({'source_root': str(other)}, {'isolated': '0'}, {'harness_session_id': CHILD},
                        {'harness_session_id': None}, {'isolated': None}, {'source_root': None}):
            with self.subTest(changes=changes):
                self.record_file(**changes)
                mod, prepared = self.prepare_as()
                self.assertIsNone(prepared.inherited)
                prepared.close()

    def test_unsafe_record_files_are_default(self):
        path = self.record_file(); path.chmod(0o644)
        mod, prepared = self.prepare_as(); self.assertIsNone(prepared.inherited); prepared.close()
        path.chmod(0o600); os.link(path, self.base / 'link')
        mod, prepared = self.prepare_as(); self.assertIsNone(prepared.inherited); prepared.close()
        (self.base / 'link').unlink(); path.unlink()
        real = self.base / 'real-record'; real.write_text(self.record_file().read_text()); real.chmod(0o600)
        (self.records / ('claude-' + NATIVE)).unlink()
        (self.records / ('claude-' + NATIVE)).symlink_to(real)
        mod, prepared = self.prepare_as(); self.assertIsNone(prepared.inherited)

    def test_owner_matches_case_insensitively_and_source_by_realpath(self):
        link = self.base / 'source-link'; link.symlink_to(self.source, target_is_directory=True)
        self.record_file(harness_session_id=OWNER.upper(), source_root=str(link))
        mod, prepared = self.prepare_as()
        self.assertEqual(prepared.inherited['permission'], 'bypassPermissions')

    def test_record_swap_after_prepare_is_refused(self):
        self.record_file()
        mod, prepared = self.prepare_as()
        self.record_file(permission='plan')
        with self.assertRaises(ValueError): prepared.recheck()
        self.assertEqual(prepared.fds, ())

    def test_record_removed_or_appearing_after_prepare_is_refused(self):
        path = self.record_file()
        mod, prepared = self.prepare_as()
        path.unlink()
        with self.assertRaises(ValueError): prepared.recheck()
        mod, prepared = self.prepare_as()
        self.record_file()
        with self.assertRaises(ValueError): prepared.recheck()

    def test_unchanged_record_rechecks(self):
        self.record_file()
        mod, prepared = self.prepare_as()
        prepared.recheck()

    def test_explicit_flags_and_keywords_win(self):
        self.record_file()
        mod, prepared = self.prepare_as()
        def rewritten(*args):
            return mod.rewrite(list(args) + ['resume', NATIVE, '--fork-session'], prepared.receipt, prepared.inherited)
        out = rewritten('--permission-mode', 'plan')
        self.assertEqual(out.count('--permission-mode'), 1); self.assertEqual(out[out.index('--permission-mode') + 1], 'plan')
        self.assertIn('--model', out)
        for flag in ('bypass', 'acceptEdits', 'dontAsk'):
            out = rewritten(flag)
            self.assertNotIn('--permission-mode', out); self.assertIn('--model', out)
        out = rewritten('--model', 'sonnet')
        self.assertEqual(out.count('--model'), 1); self.assertEqual(out[out.index('--model') + 1], 'sonnet'); self.assertIn('--effort', out)
        self.assertNotIn('claude-opus-4-8[1m]', out)
        out = rewritten('--effort', 'low')
        self.assertEqual(out.count('--effort'), 1); self.assertIn('--model', out)
        out = rewritten('--effort=low'); self.assertNotIn('--effort', out); self.assertIn('--effort=low', out)
        out = rewritten('--model=sonnet'); self.assertNotIn('--model', out); self.assertIn('--model=sonnet', out)
        for word in ('opus', 'fast', 'base', 'rich', 'fable', 'plan'):
            out = rewritten(word)
            self.assertNotIn('--model', out); self.assertNotIn('--effort', out); self.assertIn('--permission-mode', out)
        out = rewritten('high', 'opus'); self.assertNotIn('--model', out); self.assertNotIn('--effort', out)
        for word in ('low', 'medium', 'high', 'xhigh', 'max'):
            out = rewritten(word)
            self.assertNotIn('--effort', out); self.assertIn('--model', out)
        out = rewritten('ultracode'); self.assertNotIn('--model', out); self.assertNotIn('--effort', out)

    def test_flags_after_prompt_separator_are_prompt_text(self):
        self.record_file()
        mod, prepared = self.prepare_as()
        out = mod.rewrite(['resume', NATIVE, '--fork-session', '--', '--model', 'x', '--effort', 'low'], prepared.receipt, prepared.inherited)
        self.assertIn('claude-opus-4-8[1m]', out); self.assertIn('high', out)

    def test_inherited_without_probe_data_still_gives_permission(self):
        self.record_file(context=None)
        self.path.write_text(json.dumps({'sessionId': NATIVE, 'cwd': str(self.root)}) + '\n')
        mod, prepared = self.prepare_as()
        out = mod.rewrite(['resume', NATIVE, '--fork-session'], prepared.receipt, prepared.inherited)
        self.assertEqual(out, ['--passthrough', '--resume', str(self.path), '--fork-session', '--permission-mode', 'bypassPermissions'])

    def test_context_suffix_not_duplicated(self):
        self.record_file()
        self.path.write_text(self.path.read_text().replace('"input_tokens": 10', '"input_tokens": 300000'))
        mod, prepared = self.prepare_as()
        out = mod.rewrite(['resume', NATIVE, '--fork-session'], prepared.receipt, prepared.inherited)
        self.assertEqual(out[out.index('--model') + 1], 'claude-opus-4-8[1m]')


for _name in [n for n in dir(ClaudeForkTests) if n.startswith('test_')]:
    setattr(ForkInheritanceTests, _name, None)  # reuse the fixture only

if __name__ == '__main__': unittest.main()
