#!/usr/bin/env bash
# Every executed boundary is an absolute disposable fixture or this repository's
# metadata code. No installed native CLI, real registry or credential home.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Admit only the pinned synthetic default or an explicitly equal owned source.
HARNESS_TEST_OMP_SOURCE="$(python3 -I -S -B "$ROOT/test/omp-fixture-source.py")"
export HARNESS_TEST_OMP_SOURCE
python3 -I -S -B - "$ROOT" "$@" <<'PYTEST'
import copy, fcntl, hashlib, importlib.util, json, os, shlex, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(sys.argv.pop(1))
def digest(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def encoded(v): return json.dumps(v, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
def put(p, v, mode=0o600): p.write_bytes(encoded(v)); p.chmod(mode)

class ContinuationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='omp-continuation-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.home = self.base/'home'; self.home.mkdir(mode=0o700)
        env = patch.dict(os.environ, {'HOME':str(self.home)}, clear=False)
        env.start(); self.addCleanup(env.stop)
        self.registry = self.home/'.config/harness-launcher/profiles'
        self.registry.mkdir(parents=True, mode=0o700)
        self.profile = self.base/'registered project'
        (self.profile/'config').mkdir(parents=True)
        (self.profile/'config/launcher.env').write_text('HARNESS_PREFIX=ex\n')
        (self.registry/'ex').write_text(str(self.profile)+'\n'); (self.registry/'ex').chmod(0o600)
        self.calls = self.base/'native-calls'
        self.binary = self.base/'fake-native'
        self.binary.write_text('#!/bin/sh\nprintf "%s\\n" "$@" >> '+shlex.quote(str(self.calls))+'\nexit 0\n')
        self.binary.chmod(0o555)
        self.ns = self.base/'namespace'; self.ns.mkdir(mode=0o700)
        for leaf in ('home','agent','history','cwd','tmp','cache','config','state','data','native-cache'):
            (self.ns/leaf).mkdir(mode=0o700)
        put(self.ns/'.owner.json', {'schema':'p0-normal-omp-owner-v1','prefix':str(self.ns),
            'binary':str(self.binary),'asset_sha256':digest(self.binary),'identity':{'owner':'fixture'}})
        runtime = self.home/'.local/state/kilhyeonjun-harness/launcher-runtimes'
        self.snapshot = runtime/('a'*40); (self.snapshot/'bin').mkdir(parents=True)
        for p in (REPO/'bin').iterdir():
            if p.is_file():
                target=self.snapshot/'bin'/p.name; shutil.copyfile(p,target)
                target.chmod(0o500 if p.stat().st_mode&0o111 else 0o400)
        self.consumer=self.snapshot/'bin/aliases.zsh'
        self.reseal()
        spec=importlib.util.spec_from_file_location('projection',REPO/'bin/harness_omp_projection.py')
        self.projection=importlib.util.module_from_spec(spec); spec.loader.exec_module(self.projection)
        self.provenance={'binding_sha256':'1'*64,'review_sha256':'2'*64,'selection_sha256':'3'*64,
            'operation_sha256':'4'*64,'controller':{'native':'fixture','pane':'fixture','managed':'fixture'},
            'scope':'OMP3_CONFIGURATION','ssot':None}
        self.publisher=self.projection.Publisher(); self.publisher.prepare(self.provenance)
        self.release={'version':'1.2.3','source_revision':'b'*40}
        self.expiry='2099-01-01T00:00:00Z'
        self.addCleanup(self.restore_modes)

    def restore_modes(self):
        for p in [self.base,*self.base.rglob('*')]:
            if not p.is_symlink():
                try: p.chmod(0o700 if p.is_dir() else 0o600)
                except OSError: pass

    def reseal(self):
        manifest=self.snapshot/'manifest.json'
        if manifest.exists(): manifest.chmod(0o600)
        files={str(p.relative_to(self.snapshot)):{'mode':p.stat().st_mode&0o777,'sha256':digest(p)}
               for p in (self.snapshot/'bin').iterdir() if p.is_file()}
        put(manifest, {'version':'0.64.0','revision':'a'*40,'files':files},0o400)
        put(self.snapshot.parent/'current.json', {'version':'0.64.0','revision':'a'*40,
            'path':str(self.snapshot),'manifest_sha256':digest(manifest)})

    def enroll(self, ssot=False):
        if ssot:
            self.enroll()
            self.publisher.block('ex',self.publisher.token(),self.provenance,'fixture handover')
        provenance=copy.deepcopy(self.provenance)
        if ssot: provenance['ssot']={'revision_sha256':'5'*64,'export_sha256':'6'*64,
            'acl_sha256':'7'*64,'valid_until':self.expiry}
        row=self.projection.capture_row('ex',str(self.profile),str(self.consumer),str(self.binary),
            str(self.ns),self.expiry,self.release,provenance,
            lane='SSOT_ACTIVE' if ssot else 'BOOTSTRAP_ACTIVE',bootstrap_retired=ssot)
        self.publisher.publish('ex',row,self.publisher.token())

    def context(self, expected=0, consumer=None):
        # The only child here is isolated repository metadata code; no native.
        result=subprocess.run([sys.executable,'-I','-S','-B',str(self.snapshot/'bin/harness_omp_projection.py'),
            'select-continuation-source',str(self.profile),str(self.registry),str(self.profile),
            str(consumer or self.consumer)],env={'HOME':str(self.home),'PATH':'/usr/bin:/bin','LC_ALL':'C'},
            capture_output=True,timeout=10)
        self.assertEqual(result.returncode,expected,result.stderr.decode())
        self.assertFalse(self.calls.exists())
        return json.loads(result.stdout) if not expected else result.stderr

    def test_C05_bootstrap_typed_source_context(self):
        self.enroll(); context=self.context()
        self.assertEqual(context['schema'],'omp-continuation-context/v1')
        self.assertEqual(context['binary_path'],str(self.binary))
        self.assertEqual(context['binary_sha256'],digest(self.binary))
        self.assertEqual(context['namespace_path'],str(self.ns))
        self.assertEqual(context['snapshot_revision'],'a'*40)
        self.assertEqual(context['consumer_sha256'],digest(self.consumer))
        self.assertEqual(context['lane'],'BOOTSTRAP_ACTIVE')
        self.assertEqual(context['enrollment_generation'],1)

    def test_C05_ssot_is_independently_admitted(self):
        self.enroll(ssot=True); self.assertEqual(self.context()['lane'],'SSOT_ACTIVE')

    def test_C07_never_enrolled_has_no_continuation_fallback(self):
        self.context(2)

    def test_C06_current_block_then_legitimate_refresh(self):
        self.enroll(); old=self.context()
        self.publisher.block('ex',self.publisher.token(),self.provenance,'fixture')
        self.context(2); self.enroll(); new=self.context()
        self.assertGreater(new['index_generation'],old['index_generation'])
        self.assertEqual(new['namespace_identity'],old['namespace_identity'])

    def test_C08_ambiguous_registration_is_not_authority(self):
        self.enroll(); (self.registry/'duplicate').write_text(str(self.profile)+'\n')
        self.context(2)

    def test_C09_old_consumer_cannot_be_substituted(self):
        self.enroll(); self.context(2,self.base/'old/bin/aliases.zsh')

    def test_C07_context_generation_upper_bound_is_typed(self):
        self.enroll();index=self.home/'.config/harness-launcher/omp-projection-v1/enrollment.json'
        row=json.loads(index.read_bytes());row['generation']=2**63;put(index,row)
        self.context(2)

    def test_C07_core_callable_context_uses_fixed_continuation_error(self):
        self.enroll();m=self.continuation()
        self.publisher.block('ex',self.publisher.token(),self.provenance,'fixture revoked')
        with self.assertRaises(m.ContinuationError):m.current_context(self.profile,self.consumer)

    def continuation(self):
        path=self.snapshot/'bin/harness_omp_continuation.py'
        self.assertTrue(path.is_file(),'the packaged continuation metadata implementation is required')
        spec=importlib.util.spec_from_file_location('continuation',path)
        module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        return module

    def kernel(self):
        self.enroll(); self.m=self.continuation()
        self.w='12345678-1234-4234-8234-123456789ABC'; self.h=self.w.replace('-','').lower()
        self.state=self.snapshot.parent/'session-state'; self.record_dir=self.state/'sessions'/self.w
        self.record_dir.mkdir(parents=True,mode=0o700)
        self.work=self.state/'worktrees'/self.w; self.work.mkdir(parents=True,mode=0o700)
        for name,value in [('source-root',str(self.profile)+'\n'),('session-root',str(self.work)+'\n'),
                           ('lease-v1','1\n'),('runtime.lock','')]:
            (self.record_dir/name).write_text(value); (self.record_dir/name).chmod(0o600)
        bindings=self.snapshot.parent/'bindings'; bindings.mkdir(mode=0o700)
        current=json.loads((self.snapshot.parent/'current.json').read_bytes())
        put(bindings/(self.w.lower()+'.json'),{'source_root':str(self.profile),'runtime':current})
        row=self.m.initial_record(str(self.state),str(self.profile),self.w,str(self.consumer))
        self.record=self.record_dir/'omp-continuation.json';put(self.record,row)
        self.slot=self.ns/'launcher-continuations'/self.h; self.sessions=self.slot/'sessions'
        return row

    def payload(self, mode, terminal=None):
        row=json.loads(self.record.read_bytes())
        return {'schema':'omp-continuation-operation/v1','mode':mode,
            'state_root':str(self.state),'source_root':str(self.profile),'launcher_session_id':self.w,
            'consumer':str(self.consumer),'handle':self.h,'expected_sha256':digest(self.record),
            'lifecycle_nonce':row['lifecycle_nonce'],'terminal_status':terminal}

    def op(self, mode, terminal=None):
        return self.m.operate(self.payload(mode, terminal))

    def native_file(self, **changes):
        native='eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee'
        header={'type':'session','version':3,'id':native,'cwd':str(self.work),'timestamp':'2026-01-01T00:00:00.000Z'}
        header.update(changes)
        path=self.sessions/('fixture_'+native+'.jsonl')
        path.write_bytes(b'private title not retained\n'+encoded(header)+b'\nBODY_MUST_NOT_BE_READ\n');path.chmod(0o600)
        return path

    def test_C03_C04_exact_lexical_wrapper_grammar(self):
        m=self.continuation(); prompt='--resume claude "quoted"\nsecond line'
        parsed=m.request(['omp','--session-new','--model','literal-model','--',prompt])
        self.assertEqual(parsed,{'mode':'NEW','handle':None,'options':['--model','literal-model'],'prompt':[prompt]})
        self.assertIsNone(m.request(['omp','--help']))
        for args in [ ['omp','--session-resume','native-uuid','--'],
                      ['omp','--session-new','--session-new','--'],
                      ['omp','--session-new','--session-dir','/foreign','--'],
                      ['omp','--session-new','--','one','two'],
                      ['--cwd','/foreign','omp','--session-new','--'],
                      ['omp','--print','--session-new','--'] ]:
            with self.subTest(args=args),self.assertRaises(m.ContinuationError):m.request(args)

    def test_C01_kernel_origin_is_single_and_immutable(self):
        row=self.kernel(); self.assertEqual(row['handle'],self.h); self.assertEqual(row['status'],'PENDING')
        origin=copy.deepcopy(row['kernel_origin']); self.op('NEW_PREPARE')
        self.assertTrue(self.sessions.is_dir()); self.assertFalse((self.slot/'continuation.json').exists())
        self.native_file(); result=self.op('FINALIZE',0)
        self.assertEqual(result['status'],'READY')
        self.assertEqual(json.loads(self.record.read_bytes())['kernel_origin'],origin)
        self.assertEqual(self.record.stat().st_mode&0o777,0o600)

    def test_C02_public_metadata_allows_append_not_inode_replacement(self):
        self.kernel(); self.op('NEW_PREPARE'); path=self.native_file();self.op('FINALIZE',0)
        self.op('RESUME_ADMIT')
        with path.open('ab') as f:f.write(b'append\n')
        self.assertEqual(self.op('FINALIZE',0)['status'],'READY')
        self.op('RESUME_ADMIT');replacement=path.with_suffix('.replacement')
        replacement.write_bytes(path.read_bytes());replacement.chmod(0o600);os.replace(replacement,path)
        with self.assertRaises(self.m.ContinuationError):self.op('FINALIZE',0)
        self.assertEqual(json.loads(self.record.read_bytes())['status'],'BLOCKED')

    def test_C13_C26_only_fixed_kernel_record_and_no_caller_authority(self):
        self.kernel(); self.op('NEW_PREPARE');self.native_file();self.op('FINALIZE',0)
        row=json.loads(self.record.read_bytes());row['approved']=True;put(self.record,row)
        with self.assertRaises(self.m.ContinuationError):self.m.resolve(str(self.state),str(self.profile),self.h,str(self.consumer))

    def test_C14_receipt_symlink_and_hardlink_are_refused(self):
        self.kernel();other=self.record.with_name('other');os.link(self.record,other)
        with self.assertRaises(self.m.ContinuationError):self.op('NEW_PREPARE')
        other.unlink();self.record.rename(other);self.record.symlink_to(other)
        with self.assertRaises(self.m.ContinuationError):self.m.resolve(str(self.state),str(self.profile),self.h,str(self.consumer))

    def test_C15_C16_C17_header_bounds_and_cardinality(self):
        self.kernel();self.op('NEW_PREPARE')
        with self.assertRaises(self.m.ContinuationError):self.m.inspect_native(self.sessions,self.work)
        path=self.native_file(); good=path.read_bytes()
        with self.assertRaises(self.m.ContinuationError):
            self.native_file(parentSession='/foreign');self.m.inspect_native(self.sessions,self.work)
        for raw in [b'title\n'+b'x'*65537,good.replace(b'"version":3',b'"version":true'),
                    good.replace(b'"type":"session"',b'"type":"session","type":"session"'),
                    good.split(b'\n')[0]+b'\n{}',b'one line\n']:
            path.write_bytes(raw)
            with self.subTest(raw=raw[:30]),self.assertRaises(self.m.ContinuationError):self.m.inspect_native(self.sessions,self.work)
        path.write_bytes(good);second=self.sessions/'second.jsonl';second.write_bytes(good);second.chmod(0o600)
        with self.assertRaises(self.m.ContinuationError):self.m.inspect_native(self.sessions,self.work)
        second.unlink()
        for i in range(16):(self.sessions/str(i)).write_bytes(b'')
        with self.assertRaises(self.m.ContinuationError):self.m.inspect_native(self.sessions,self.work)

    def test_C19_C21_native_fork_and_nonzero_never_ready(self):
        self.kernel();self.op('NEW_PREPARE');self.native_file(parentSession='/foreign')
        with self.assertRaises(self.m.ContinuationError):self.op('FINALIZE',0)
        self.assertEqual(json.loads(self.record.read_bytes())['status'],'BLOCKED')

    def test_C22_unknown_terminal_is_not_resumable_or_retried(self):
        self.kernel();self.op('NEW_PREPARE');self.native_file();self.op('FINALIZE',0)
        self.op('RESUME_ADMIT');before=self.record.read_bytes()
        self.assertEqual(self.op('FINALIZE',None)['status'],'IN_FLIGHT');self.assertEqual(self.record.read_bytes(),before)
        with self.assertRaises(self.m.ContinuationError):self.m.resolve(str(self.state),str(self.profile),self.h,str(self.consumer))

    def test_C12_C23_retired_workspace_not_recreated(self):
        self.kernel(); self.work.rmdir()
        with self.assertRaises(self.m.ContinuationError):self.op('NEW_PREPARE')
        self.assertFalse(self.work.exists());self.assertFalse(self.sessions.exists())

    def test_C06_origin_generation_is_history_current_authority_is_live(self):
        self.kernel();self.op('NEW_PREPARE');self.native_file();self.op('FINALIZE',0)
        origin=json.loads(self.record.read_bytes())['origin_context_provenance']
        self.publisher.block('ex',self.publisher.token(),self.provenance,'fixture block')
        with self.assertRaises(self.m.ContinuationError):self.m.resolve(str(self.state),str(self.profile),self.h,str(self.consumer))
        self.enroll();self.op('RESUME_ADMIT');self.op('FINALIZE',0)
        row=json.loads(self.record.read_bytes())
        self.assertEqual(row['origin_context_provenance'],origin)
        self.assertGreater(row['last_admitted_context_provenance']['index_generation'],origin['index_generation'])

    def test_C15_timestamp_uuid_and_private_parent_modes(self):
        self.kernel();self.op('NEW_PREPARE');path=self.native_file();good=path.read_bytes()
        for changes in [{'timestamp':'not a timestamp'},{'id':'not-a-uuid'},{'version':True},{'cwd':str(self.profile)}]:
            with self.subTest(changes=changes):
                self.native_file(**changes)
                with self.assertRaises(self.m.ContinuationError):self.m.inspect_native(self.sessions,self.work)
        path.write_bytes(good);self.slot.chmod(0o755)
        with self.assertRaises(self.m.ContinuationError):self.m.inspect_native(self.sessions,self.work)

    def test_C17_known_terminal_without_persistence_blocks_slot(self):
        self.kernel();self.op('NEW_PREPARE')
        with self.assertRaises(self.m.ContinuationError):self.op('FINALIZE',0)
        row=json.loads(self.record.read_bytes());self.assertEqual(row['status'],'BLOCKED')
        self.assertIsNone(row['native_identity'])

    def test_C18_cas_and_mutation_during_bounded_read_fail_without_retry(self):
        self.kernel();payload=self.payload('NEW_PREPARE');row=json.loads(self.record.read_bytes())
        row['last_observation_grade']='POSTCHECK_FAILED';put(self.record,row)
        with self.assertRaises(self.m.ContinuationError):self.m.operate(payload)
        self.assertFalse(self.sessions.exists())
        self.op('NEW_PREPARE');path=self.native_file();original_read=os.read;changed=[]
        def mutate(fd,n):
            raw=original_read(fd,n)
            if not changed:
                changed.append(True)
                with path.open('ab') as f:f.write(b'concurrent append\n')
            return raw
        with patch.object(os,'read',side_effect=mutate),self.assertRaises(self.m.ContinuationError):
            self.m.inspect_native(self.sessions,self.work)

    def test_C08_C09_changed_origin_binding_and_helper_cannot_be_admitted(self):
        self.kernel();row=json.loads(self.record.read_bytes());row['kernel_origin']['binary_sha256']='0'*64;put(self.record,row)
        with self.assertRaises(self.m.ContinuationError):self.op('NEW_PREPARE')
        helper=self.snapshot/'bin/harness_omp_continuation.py';helper.chmod(0o600)
        helper.write_text('raise RuntimeError("MUST_NOT_IMPORT")\n');helper.chmod(0o400)
        self.context(2)

    def public_shell(self, tail, expected):
        # Public alias-layer fixture only. The required real Entry composition
        # is separate and cannot be replaced by this seeded-kernel fixture.
        script='''source "$1"
harness_python3_resolve() { print -r -- "$FIXTURE_PYTHON"; }
_harness_launcher_isolated_session_create() { print -r -- create >> "$FIXTURE_EVENTS"; return 97; }
_harness_launcher_isolated_finish() {
  print -r -- finish >> "$FIXTURE_EVENTS"
  [[ -z "$3" ]] || zsystem flock -u "$3"
}
shift
_harness_launcher_run "$@"
'''
        env={'HOME':str(self.home),'PATH':str(self.tools)+':/usr/bin:/bin','LC_ALL':'C','TERM':'dumb',
             'HARNESS_SESSION_STATE_HOME':str(self.state),'FIXTURE_PYTHON':sys.executable,
             'FIXTURE_EVENTS':str(self.events),'HARNESS_CLAUDE_PLUGIN_MIRROR':'0',
             'HARNESS_POST_EDIT_CODEX_SYNC':'0','HARNESS_POST_COMMIT_CODEX_SYNC':'0','TMPDIR':str(self.base)}
        self.assertEqual(self.binary.parent,self.base)
        self.assertTrue(self.binary.read_text().startswith('#!'+sys.executable+' -I'))
        self.assertEqual((self.snapshot/'bin/harness_omp_continuation.py').read_bytes(),
                         (REPO/'bin/harness_omp_continuation.py').read_bytes())
        done=subprocess.run(['/bin/zsh','-f','-c',script,'fixture',str(self.consumer),str(self.profile),
            '--isolated-session',self.w,'omp',*tail],cwd=self.profile,env=env,capture_output=True,timeout=20)
        self.assertEqual(done.returncode,expected,done.stderr.decode())
        return done

    def public_fixture(self, native_exit=0):
        self.tools=self.base/'tools';self.tools.mkdir();self.events=self.base/'events'
        for name in ('omp','claude','codex','kiro-cli','kiro'):
            tool=self.tools/name;tool.write_text('#!/bin/sh\nexit 97\n');tool.chmod(0o700)
        # Prevent even an unexpected generic branch from entering installed
        # engines/GC: the staged session dispatcher is an absolute denial stub.
        engine=self.snapshot/'bin/session-isolation.sh';engine.chmod(0o700)
        engine.write_text('#!/bin/sh\nprintf "%s\\n" "$@" >> '+shlex.quote(str(self.events))+'\nexit 97\n');engine.chmod(0o500)
        self.reseal()
        self.binary.chmod(0o755)
        self.binary.write_text('#!'+sys.executable+' -I\n'+'''
import errno,fcntl,json,os,sys
from pathlib import Path
args=sys.argv[1:];sessions=Path(args[args.index('--session-dir')+1]);cwd=Path.cwd()
native='eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee'
target=sessions/('fixture_'+native+'.jsonl')
if '--resume' in args:
    assert args[args.index('--resume')+1]==str(target)
    with target.open('ab') as f:f.write(b'APPENDED\\n')
else:
    header={'type':'session','version':3,'id':native,'cwd':str(cwd),'timestamp':'2026-01-01T00:00:00.000Z'}
    fd=os.open(target,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    with os.fdopen(fd,'w') as f:f.write('title\\n'+json.dumps(header)+'\\n')
held=[]
for lock in [Path(LOCK_WORKSPACE),Path(LOCK_NAMESPACE)]:
    fd=os.open(lock,os.O_RDWR)
    try:
        try:fcntl.lockf(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);held.append(False)
        except OSError as exc:
            assert exc.errno in (errno.EACCES,errno.EAGAIN);held.append(True)
    finally:os.close(fd)
proof={'argv':args,'cwd':str(cwd),'held':held,'env':dict(os.environ)}
with Path(CALLS).open('a') as f:f.write(json.dumps(proof)+'\\n')
raise SystemExit(EXIT_CODE)
'''.replace('LOCK_WORKSPACE',repr(str(self.snapshot.parent/'session-state/sessions/12345678-1234-4234-8234-123456789ABC/runtime.lock')))
   .replace('LOCK_NAMESPACE',repr(str(self.ns/'.launcher.lock'))).replace('CALLS',repr(str(self.calls))).replace('EXIT_CODE',str(native_exit)))
        self.binary.chmod(0o555)
        marker=json.loads((self.ns/'.owner.json').read_bytes());marker['asset_sha256']=digest(self.binary)
        put(self.ns/'.owner.json',marker)
        # Mandatory paired W07 cascade: this normal alias fixture now needs a
        # source-equal structural kernel and an explicitly fake amended release.
        kernel=Path(os.environ['HARNESS_TEST_OMP_SOURCE'])
        self.release={'version':'18.11.3-kh.w07.1','source_revision':'1'*40,
            'contract_id':'w07.required-resolver-consumer.v1','upstream_base_version':'18.11.3',
            'upstream_base_revision':'96894a6147d8cc64b7e00ad3a7d7178fd93b9531',
            'source_patch_sha256':'2'*64,'build_provenance_sha256':'3'*64}
        def source_file(name,body):
            path=self.profile/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(body);path.chmod(0o600)
        for name in ('runtime-contract.yaml','budgets.yaml','claude-md.yaml'):
            body=(kernel/'.claude/source'/name).read_text()
            if name=='runtime-contract.yaml':
                body='\n'.join(line for line in body.splitlines() if 'shared_contract_release:' not in line)+'\n'
            source_file('.claude/source/'+name,body)
        for kind in ('agents','commands','skills'):
            source_file('.claude/source/'+kind+'.yaml',kind+':\n  source: .claude/'+kind+'\n')
        source_file('.claude/source/settings.yaml',
            (kernel/'.claude/source/settings.yaml').read_text().split('\nhooks:',1)[0]+
            '\nhooks:\n  omp_required_contract: w07.required-resolver-consumer.v1\n  omp_upstream_base_version: "18.11.3"\n'
            '  omp_upstream_base_revision: 96894a6147d8cc64b7e00ad3a7d7178fd93b9531\n  entries:\n')
        for name in ('core/scripts/harness_compile.py','core/scripts/omp_required_extension.ts'):
            source_file(name,(kernel/name).read_text())
        (self.profile/'.claude/rules').mkdir()
        self.kernel()
        (self.snapshot/'bin').chmod(0o500);self.snapshot.chmod(0o500)

    def test_C01_C03_C27_public_alias_layer_fake_only(self):
        self.public_fixture()
        prompt='literal --resume claude "quoted"\nline two'
        self.public_shell(['--session-new','--print','--',prompt],0)
        calls=[json.loads(x) for x in self.calls.read_text().splitlines()]
        bundle=next((self.profile/'.harness/omp').iterdir())
        required=['--append-system-prompt='+str(bundle/'policy.md'),'--no-extensions',
                  '--extension='+str(bundle/'required-extension.ts'),'--harness-required-bundle='+str(bundle/'selection.json')]
        self.assertEqual(len(calls),1);self.assertEqual(calls[0]['argv'],['--print',*required,'--session-dir',str(self.sessions),'--',prompt])
        self.assertEqual(calls[0]['cwd'],str(self.work));self.assertEqual(calls[0]['held'],[True,True])
        self.assertEqual(self.events.read_text(),'finish\n')
        self.public_shell(['--session-resume',self.h,'--print','--',prompt],0)
        calls=[json.loads(x) for x in self.calls.read_text().splitlines()]
        native=str(self.sessions/'fixture_eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee.jsonl')
        self.assertEqual(calls[1]['argv'],['--print',*required,'--session-dir',str(self.sessions),'--resume',native,'--',prompt])
        self.assertEqual(calls[1]['cwd'],str(self.work));self.assertEqual(calls[1]['held'],[True,True])
        self.assertEqual(self.events.read_text(),'finish\n')
        expected_env={'HOME':str(self.ns/'home'),'PATH':'/usr/bin:/bin','LANG':'C','LC_ALL':'C',
            'TMPDIR':str(self.ns/'tmp'),'XDG_CACHE_HOME':str(self.ns/'cache'),'XDG_CONFIG_HOME':str(self.ns/'config'),
            'XDG_STATE_HOME':str(self.ns/'state'),'XDG_DATA_HOME':str(self.ns/'data'),
            'PI_CODING_AGENT_DIR':str(self.ns/'agent'),'PI_CODING_AGENT_SESSION_DIR':str(self.ns/'history'),
            'PI_NATIVES_DIR':str(self.ns/'native-cache'),'TERM':'xterm-256color'}
        for call in calls:
            self.assertEqual({k:call['env'][k] for k in expected_env},expected_env)
            # Darwin may add this Python-startup key after env -i. Production
            # still sends exactly13; this observation is not a wider env grant.
            self.assertLessEqual(set(call['env'])-set(expected_env),{'__CF_USER_TEXT_ENCODING'})

    def test_C10_namespace_busy_no_native_or_receipt_transition(self):
        self.public_fixture();lock=self.ns/'.launcher.lock';lock.write_bytes(b'');lock.chmod(0o600)
        fd=os.open(lock,os.O_RDWR)
        try:
            fcntl.lockf(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            before=self.record.read_bytes();self.public_shell(['--session-new','--'],2)
            self.assertEqual(self.record.read_bytes(),before);self.assertFalse(self.calls.exists())
            self.assertFalse(self.sessions.exists());self.assertEqual(self.events.read_text(),'finish\n')
        finally:os.close(fd)

    def test_C11_workspace_busy_precedes_namespace_and_native(self):
        self.public_fixture();fd=os.open(self.record_dir/'runtime.lock',os.O_RDWR)
        try:
            fcntl.lockf(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.public_shell(['--session-new','--'],2)
            self.assertFalse((self.ns/'.launcher.lock').exists());self.assertFalse(self.calls.exists())
            self.assertFalse(self.events.exists())
        finally:os.close(fd)

    def test_C21_foreground_native_nonzero_preserved(self):
        self.public_fixture(native_exit=7)
        done=self.public_shell(['--session-new','--print','--','literal'],7)
        self.assertNotIn(b'OMP_CONTINUATION_HANDLE=',done.stderr)
        row=json.loads(self.record.read_bytes());self.assertEqual(row['status'],'BLOCKED')
        self.assertEqual(row['last_terminal_status'],7);self.assertEqual(self.events.read_text(),'finish\n')

if __name__=='__main__': unittest.main(verbosity=2)
PYTEST
