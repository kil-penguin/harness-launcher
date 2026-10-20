#!/usr/bin/env bash
# All native boundaries below are absolute disposable stubs, never installed CLIs.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
python3 -I -S -B - "$ROOT" <<'PYTEST'
import copy, hashlib, importlib.util, json, os, platform, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(sys.argv.pop())
HELPER = REPO / 'bin/harness_omp_projection.py'

def digest(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def encoded(v): return json.dumps(v, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
def put(p, v, mode=0o600):
    p.write_bytes(encoded(v)); p.chmod(mode)

class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='omp-projection-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.home = self.base / 'home'; self.home.mkdir(mode=0o700)
        self.environment = patch.dict(os.environ, {'HOME': str(self.home)}, clear=False)
        self.environment.start(); self.addCleanup(self.environment.stop)
        self.registry = self.home / '.config/harness-launcher/profiles'
        self.registry.mkdir(parents=True, mode=0o700)
        self.profile = self.base / 'registered project'; (self.profile/'config').mkdir(parents=True)
        (self.registry/'ex').write_text(str(self.profile)+'\n'); (self.registry/'ex').chmod(0o600)
        self.other = self.base/'other'; (self.other/'config').mkdir(parents=True)
        (self.registry/'other').write_text(str(self.other)+'\n'); (self.registry/'other').chmod(0o600)
        self.calls = self.base/'native-calls'
        self.binary = self.base/'fake-native'
        # Shell builtin writes literal argv only; cannot reach any real CLI.
        self.binary.write_text('#!/bin/sh\nprintf \'%s\\n\' "$@" >> '+str(self.calls)+'\nexit 0\n')
        self.binary.chmod(0o555)
        self.ns=self.base/'namespace'; self.ns.mkdir(mode=0o700)
        for leaf in ('home','agent','history','cwd','tmp','cache','config','state','data','native-cache'):
            (self.ns/leaf).mkdir(mode=0o700)
        put(self.ns/'.owner.json', {'schema':'p0-normal-omp-owner-v1','prefix':str(self.ns),'binary':str(self.binary),'asset_sha256':digest(self.binary),'identity':{'owner':'fixture'}})
        self.runtime=self.home/'.local/state/kilhyeonjun-harness/launcher-runtimes'
        self.snapshot=self.runtime/('a'*40); (self.snapshot/'bin').mkdir(parents=True)
        # Match managed stager's tracked-bin inclusion, with no fixed file count.
        for p in (REPO/'bin').iterdir():
            if p.is_file():
                target=self.snapshot/'bin'/p.name;shutil.copyfile(p,target);target.chmod(0o500 if p.stat().st_mode&0o111 else 0o400)
        self.consumer=self.snapshot/'bin/aliases.zsh'
        files={str(p.relative_to(self.snapshot)):{'mode':p.stat().st_mode&0o777,'sha256':digest(p)} for p in (self.snapshot/'bin').iterdir()}
        put(self.snapshot/'manifest.json',{'version':'0.64.0','revision':'a'*40,'files':files},0o400)
        put(self.runtime/'current.json', {'version':'0.64.0','revision':'a'*40,'path':str(self.snapshot),'manifest_sha256':digest(self.snapshot/'manifest.json')})
        spec=importlib.util.spec_from_file_location('projection',HELPER)
        self.m=importlib.util.module_from_spec(spec); spec.loader.exec_module(self.m)
        self.publisher=self.m.Publisher()
        self.provenance={'binding_sha256':'1'*64,'review_sha256':'2'*64,'selection_sha256':'3'*64,'operation_sha256':'4'*64,'controller':{'native':'fixture','pane':'fixture','managed':'fixture'},'scope':'OMP3_CONFIGURATION','ssot':None}
        self.publisher.prepare(self.provenance)
        self.index=self.home/'.config/harness-launcher/omp-projection-v1/enrollment.json'
        self.state=self.index.with_name('state.json')
        self.release={'version':'1.2.3','source_revision':'b'*40}
        self.expiry='2099-01-01T00:00:00Z'
        self.addCleanup(self.restore_modes)

    def restore_modes(self):
        for p in [self.base,*self.base.rglob('*')]:
            if not p.is_symlink():
                try: p.chmod(0o700 if p.is_dir() else 0o600)
                except OSError: pass

    def row(self, profile='ex', lane='BOOTSTRAP_ACTIVE', retired=False, provenance=None):
        return self.m.capture_row(profile,str(self.profile if profile=='ex' else self.other),str(self.consumer),str(self.binary),str(self.ns),self.expiry,self.release,provenance or self.provenance,lane=lane,bootstrap_retired=retired)

    def enroll(self, profile='ex', **kw):
        self.publisher.publish(profile,self.row(profile,**kw),self.publisher.token())

    def select(self, profile='ex'):
        return self.m.select(profile,str(self.profile if profile=='ex' else self.other),str(self.consumer))

    def rejected(self, fn, label=None):
        with self.assertRaises(self.m.ProjectionError) as c: fn()
        if label: self.assertEqual(c.exception.label,label)
        self.assertFalse(self.calls.exists())

    def corrupt_row(self, edit):
        state=json.loads(self.state.read_bytes()); edit(state['profiles']['ex'])
        put(self.state,state)
        index=json.loads(self.index.read_bytes()); index['enrolled_profiles']['ex']['payload_row_sha256']=hashlib.sha256(encoded(state['profiles']['ex'])).hexdigest();put(self.index,index)

    def test_O1_never_enrolled_does_not_open_payload_and_typed_bootstrap_works(self):
        self.state.write_text('unread malformed payload');self.state.chmod(0o600)
        with patch.object(self.m,'read_file',wraps=self.m.read_file) as read:
            self.assertIsNone(self.select())
            self.assertNotIn(self.state,[Path(x.args[0]) for x in read.call_args_list])
        self.state.unlink();self.enroll()
        self.assertEqual(self.select(),{'HARNESS_OMP_BIN':str(self.binary),'HARNESS_OMP_ROOT':str(self.ns),'HARNESS_OMP_SHA256':digest(self.binary)})

    def test_O2_host_and_profile_binding_cannot_be_reclassified_absent(self):
        self.enroll(); old=self.index.read_bytes()
        for key,value in [('hostname','wrong'),('uid',True),('installation_id','0'*31),('architecture','wrong')]:
            with self.subTest(key=key):
                v=json.loads(old);v['host'][key]=value;put(self.index,v);self.rejected(self.select)
        self.index.write_bytes(old)
        (self.registry/'ex').write_text(str(self.other)+'\n');self.rejected(self.select,'OMP_BINDING_MISMATCH')

    def test_O3_binary_namespace_marker_modes_links_and_hash(self):
        self.enroll(); original=self.binary.read_bytes(); marker=(self.ns/'.owner.json').read_bytes()
        self.binary.chmod(0o755);self.rejected(self.select);self.binary.chmod(0o555)
        self.binary.chmod(0o755);self.binary.write_bytes(original+b'#changed\n');self.binary.chmod(0o555);self.rejected(self.select)
        self.binary.chmod(0o755);self.binary.write_bytes(original);self.binary.chmod(0o555)
        linked=self.base/'hardlink';os.link(self.binary,linked);self.rejected(self.select);linked.unlink()
        self.ns.chmod(0o755);self.rejected(self.select);self.ns.chmod(0o700)
        (self.ns/'.owner.json').write_text('{}');self.rejected(self.select);(self.ns/'.owner.json').write_bytes(marker)
        self.binary.rename(self.base/'original');self.binary.symlink_to(self.base/'original');self.rejected(self.select)

    def test_O4_strict_json_metadata_current_expiry_provenance(self):
        self.enroll(); original=self.index.read_bytes()
        bad=[b'{',b'\xff',b'{"schema_version":1,"schema_version":1}',b' '*65537]
        for raw in bad:
            with self.subTest(raw=raw[:15]):self.index.write_bytes(raw);self.rejected(self.select)
        self.index.write_bytes(original)
        for mutate in [lambda v:v.update(unknown=1),lambda v:v.update(schema_version=True),lambda v:v.update(generation=False),lambda v:v.update(complete_enrollment_set=False)]:
            v=json.loads(original);mutate(v);put(self.index,v);self.rejected(self.select)
        self.index.write_bytes(original);self.index.chmod(0o644);self.rejected(self.select);self.index.chmod(0o600)
        for edit in [lambda r:r.update(valid_until='2000-01-01T00:00:00Z'),lambda r:r['provenance'].update(approved=True),lambda r:r['launcher'].update(current_sha256='0'*64),lambda r:r['binary'].update(bytes=True),lambda r:r.update(bootstrap_retired=0)]:
            saved=self.state.read_bytes();index=self.index.read_bytes();self.corrupt_row(edit);self.rejected(self.select);self.state.write_bytes(saved);self.index.write_bytes(index)
        alias=self.consumer.read_bytes();self.consumer.chmod(0o600);self.consumer.write_bytes(alias+b'\n#tamper');self.consumer.chmod(0o400);self.rejected(self.select)

    def test_O5_enrolled_missing_revoked_never_falls_back_other_profile_unaffected(self):
        self.enroll();self.state.unlink();self.rejected(self.select,'OMP_PROJECTION_MISSING');self.assertIsNone(self.select('other'))
        self.index.unlink();self.rejected(self.select)
        self.rejected(lambda:self.publisher.prepare(self.provenance))

    def test_O6_handover_requires_block_first_ssot_retirement_monotonic(self):
        self.enroll();ssot=copy.deepcopy(self.provenance);ssot['ssot']={'revision_sha256':'5'*64,'export_sha256':'6'*64,'acl_sha256':'7'*64,'valid_until':self.expiry}
        row=self.row(lane='SSOT_ACTIVE',retired=True,provenance=ssot)
        self.rejected(lambda:self.publisher.publish('ex',row,self.publisher.token()))
        self.publisher.block('ex',self.publisher.token(),self.provenance,'handover')
        self.rejected(self.select,'OMP_PROJECTION_REVOKED')
        self.publisher.publish('ex',row,self.publisher.token());self.assertIsNotNone(self.select())
        self.publisher.block('ex',self.publisher.token(),ssot,'replacement-failure')
        self.rejected(lambda:self.publisher.publish('ex',self.row(),self.publisher.token()))
        self.publisher.publish('ex',row,self.publisher.token(),legacy_retired=True)
        self.assertTrue(json.loads(self.index.read_bytes())['enrolled_profiles']['ex']['legacy_retired'])
        self.rejected(lambda:self.publisher.publish('ex',row,self.publisher.token(),legacy_retired=False))
        self.publisher.block('ex',self.publisher.token(),ssot,'revoked',revoke=True)
        self.rejected(self.select,'OMP_PROJECTION_REVOKED')

    def test_CAS_lock_and_partial_publication_fail_closed_without_erasing_entries(self):
        import fcntl
        token=self.publisher.token();self.enroll()
        self.rejected(lambda:self.publisher.publish('other',self.row('other'),token))
        lock=self.index.with_name('writer.lock')
        with lock.open('rb') as held:
            fcntl.flock(held,fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.rejected(lambda:self.publisher.publish('other',self.row('other'),self.publisher.token()))
        self.enroll('other');self.assertIsNotNone(self.select());self.assertIsNotNone(self.select('other'))
        atomic=self.m.atomic_json
        def fail_index(path,value):
            if Path(path)==self.index:raise OSError('fixture index publication failure')
            return atomic(path,value)
        with patch.object(self.m,'atomic_json',side_effect=fail_index):
            self.rejected(lambda:self.publisher.publish('ex',self.row(),self.publisher.token()))
        self.rejected(self.select);self.assertEqual(set(json.loads(self.index.read_bytes())['enrolled_profiles']),{'ex','other'})

    def test_I1_I2_replacement_race_is_stop_no_retry(self):
        self.enroll();read=self.m.read_file;reads=[]
        def racing(path,*args,**kw):
            result=read(path,*args,**kw)
            if Path(path)==self.state:
                index=json.loads(self.index.read_bytes());index['generation']+=1;put(self.index,index)
            reads.append(Path(path));return result
        with patch.object(self.m,'read_file',side_effect=racing):self.rejected(self.select)
        self.assertEqual(reads.count(self.state),1)

    def test_no_implicit_repair_after_incomplete_pair_and_no_revoked_bootstrap_resurrection(self):
        self.enroll();atomic=self.m.atomic_json
        def fail_index(path,value):
            if Path(path)==self.index:raise OSError('fixture index publication failure')
            return atomic(path,value)
        with patch.object(self.m,'atomic_json',side_effect=fail_index):
            self.rejected(lambda:self.publisher.publish('ex',self.row(),self.publisher.token()))
        # A later publisher cannot adopt the mismatched payload as its base.
        self.rejected(self.publisher.token)

    def test_revoked_bootstrap_never_becomes_active_again(self):
        self.enroll();self.publisher.block('ex',self.publisher.token(),self.provenance,'revoked',revoke=True)
        self.rejected(lambda:self.publisher.publish('ex',self.row(),self.publisher.token()))

    def test_failed_first_prepare_leaves_no_usable_selector_and_cannot_reset(self):
        # Use a second disposable HOME; the established original is untouched.
        other_home=self.base/'first-rollout-home';(other_home/'.config/harness-launcher/profiles').mkdir(parents=True)
        with patch.dict(os.environ,{'HOME':str(other_home)}):
            publisher=self.m.Publisher()
            with patch.object(self.m,'atomic_json',side_effect=OSError('fixture write failure')):
                self.rejected(lambda:publisher.prepare(self.provenance))
            self.rejected(lambda:publisher.prepare(self.provenance))
            self.assertFalse(publisher.index.exists())

    def test_publish_rejects_unbound_expired_wrong_profile_and_legacy_retirement_early(self):
        row=self.row()
        for edit in [lambda r:r['provenance'].update(binding_sha256='0'*64),lambda r:r.update(valid_until='2000-01-01T00:00:00Z'),lambda r:r['profile_binding'].update(root=str(self.other)),lambda r:r['native_home']['root_identity'].update(ino=0)]:
            v=copy.deepcopy(row);edit(v)
            self.rejected(lambda:self.publisher.publish('ex',v,self.publisher.token()))
        self.rejected(lambda:self.publisher.publish('ex',row,self.publisher.token(),legacy_retired=True))

    def test_publication_rechecks_current_under_lock_and_corrupt_joins_stop(self):
        self.enroll();row=self.row();current=self.runtime/'current.json';v=json.loads(current.read_bytes());v['manifest_sha256']='0'*64;put(current,v)
        self.rejected(lambda:self.publisher.publish('ex',row,self.publisher.token()))
        # File size/type and unsafe UID are separate from JSON shape.
        self.corrupt_row(lambda r:r['binary'].update(bytes=999));self.rejected(self.select)

    def test_read_directory_link_type_unknown_keys_and_owner_tamper(self):
        self.enroll();index=self.index.read_bytes();state=self.state.read_bytes()
        for edit in [lambda r:r['profile_binding']['root_identity'].update(uid=os.getuid()+1),lambda r:r['binary'].update(uid=os.getuid()+1),lambda r:r['native_home']['owner_marker'].update(sha256='0'*64),lambda r:r['native_home'].update(path='relative'),lambda r:r.update(extra=None)]:
            self.corrupt_row(edit);self.rejected(self.select);self.state.write_bytes(state);self.index.write_bytes(index)
        self.state.unlink();self.state.mkdir(mode=0o700);self.rejected(self.select);self.state.rmdir();self.state.write_bytes(state);self.state.chmod(0o600)
        registry=self.registry/'ex';registry.chmod(0o666);self.rejected(self.select);registry.chmod(0o600)
        marker=self.ns/'.owner.json';marker.chmod(0o644);self.rejected(self.select)

    def test_strict_CAS_types_control_strings_and_receipt_join(self):
        token=self.publisher.token();token['index_generation']=True
        self.rejected(lambda:self.publisher.publish('ex',self.row(),token))
        self.enroll();old=self.state.read_bytes();index=self.index.read_bytes()
        for value in ['fixture\x85hidden','fixture\u202ehidden']:
            self.corrupt_row(lambda r:r['provenance']['controller'].update(pane=value));self.rejected(self.select)
            self.state.write_bytes(old);self.index.write_bytes(index)
        v=json.loads(index);v['enrolled_profiles']['ex']['selection_receipt']['operation_sha256']='9'*64;put(self.index,v)
        self.rejected(self.select)

    def test_symlink_ancestors_hardlinked_and_replaced_state_refused(self):
        self.enroll();copy_path=self.state.with_name('copy');os.link(self.state,copy_path);self.rejected(self.select);copy_path.unlink()
        self.state.rename(copy_path);self.state.symlink_to(copy_path);self.rejected(self.select);self.state.unlink();copy_path.rename(self.state)
        parent=self.index.parent;parent.rename(parent.with_name('away'));parent.symlink_to(parent.with_name('away'));self.rejected(self.select)

    def test_fresh_shell_managed_snapshot_legacy_typed_management_and_nonOMP(self):
        # This is the hard deny-installed gate, established BEFORE invoking zsh.
        self.assertEqual(self.binary.parent,self.base);self.assertTrue(self.binary.read_text().startswith('#!/bin/sh'))
        self.assertEqual((self.snapshot/'bin/harness_omp_projection.py').read_bytes(),HELPER.read_bytes())
        for p in [self.snapshot/'bin',self.snapshot]:p.chmod(0o500)
        config=self.profile/'config/launcher.env'
        values={'HARNESS_NAME':'fixture','HARNESS_PREFIX':'ex','HARNESS_SESSION_ISOLATION_DEFAULT':'0','HARNESS_OMP_BIN':str(self.binary),'HARNESS_OMP_ROOT':str(self.ns),'HARNESS_OMP_SHA256':digest(self.binary)}
        import shlex
        config.write_text('\n'.join(k+'='+shlex.quote(v) for k,v in values.items())+'\n')
        tools=self.base/'tools';tools.mkdir()
        for name in ('claude','codex','omp','kiro-cli'):
            p=tools/name;p.write_text('#!/bin/sh\nexit 97\n');p.chmod(0o700)
        # Bound interpreter resolver to the exact current Python. Installed
        # native paths cannot be selected: valid config pins only our fake B.
        codex_route=self.base/'codex-route'
        script='source "$1"; harness_python3_resolve() { print -r -- '+shlex.quote(sys.executable)+'; }; _harness_launcher_run_codex_cli() { shift 2; builtin printf "%s\\n" "$@" > '+shlex.quote(str(codex_route))+'; return 97; }; shift; _harness_launcher_run "$@"'
        env={'HOME':str(self.home),'PATH':str(tools)+':/usr/bin:/bin','LC_ALL':'C','TERM':'dumb','HARNESS_CLAUDE_PLUGIN_MIRROR':'0','HARNESS_POST_EDIT_CODEX_SYNC':'0','HARNESS_POST_COMMIT_CODEX_SYNC':'0','TMPDIR':str(self.base)}
        def run(args,rc):
            before=self.calls.read_bytes() if self.calls.exists() else b''
            result=subprocess.run(['/bin/zsh','-f','-c',script,'fixture',str(self.consumer),str(self.profile),*args],env=env,cwd=self.profile,capture_output=True,timeout=20)
            self.assertEqual(result.returncode,rc,result.stderr.decode())
            after=self.calls.read_bytes() if self.calls.exists() else b''
            if rc:self.assertEqual(before,after)
            return after[len(before):]
        self.assertEqual(run(['omp','--version'],0),b'--version\n')
        self.enroll();values['HARNESS_OMP_BIN']='/MUST_NOT_EXECUTE/foreign';values['HARNESS_OMP_SHA256']='0'*64
        config.write_text('\n'.join(k+'='+shlex.quote(v) for k,v in values.items())+'\n')
        for tail in [['--help'],['login','anthropic'],['models','ls','openai-codex','--json','--kind','chat','--no-extensions']]:
            self.assertEqual(run(['omp',*tail],0),('\n'.join(tail)+'\n').encode())
        self.assertFalse((self.profile/'.harness').exists())
        self.state.unlink();run(['omp','--version'],2)
        # Non-OMP management still reaches only the installed-denial stub;
        # it must not require or read the now-missing projection.
        run(['claude','auth','status'],97)
        run(['codex','--passthrough','auth','status'],97)
        self.assertEqual(codex_route.read_text(),'--passthrough\nauth\nstatus\n')

    # --- D: device renumbering tolerance (APFS renumbers st_dev at reboot) ---
    FIELDS = {
        'host': (lambda r: r['host'], lambda r: r['host']['profile_registry_directory_identity']),
        'profile_root': (lambda r: r['profile_binding'], lambda r: r['profile_binding']['root_identity']),
        'registry_file': (lambda r: r['profile_binding'], lambda r: r['profile_binding']['registry']['identity']),
        'native_home': (lambda r: r['native_home'], lambda r: r['native_home']['root_identity']),
    }

    def stored_identities(self, field, index, state):
        """Every stored copy of one identity field (index/state host; entry/row)."""
        if field == 'host':
            return [index['host']['profile_registry_directory_identity'],
                    state['host']['profile_registry_directory_identity']]
        row = state['profiles']['ex']; entry = index['enrolled_profiles']['ex']
        if field == 'profile_root':
            return [entry['profile_binding']['root_identity'], row['profile_binding']['root_identity']]
        if field == 'registry_file':
            return [entry['profile_binding']['registry']['identity'], row['profile_binding']['registry']['identity']]
        return [row['native_home']['root_identity']]

    def rewrite(self, edit):
        index = json.loads(self.index.read_bytes()); state = json.loads(self.state.read_bytes())
        edit(index, state)
        index['enrolled_profiles']['ex']['payload_row_sha256'] = hashlib.sha256(encoded(state['profiles']['ex'])).hexdigest()
        put(self.state, state); put(self.index, index)

    def renumber(self, *fields, delta=1, key='dev'):
        def edit(index, state):
            for field in fields:
                for ident in self.stored_identities(field, index, state): ident[key] += delta
        self.rewrite(edit)

    def parent_of(self, field):
        return {'host': self.registry.parent, 'profile_root': self.profile.parent,
                'registry_file': self.registry, 'native_home': self.ns.parent}[field]

    def other_device(self, parents):
        real = self.m.device
        guard = {str(p) for p in parents}
        return patch.object(self.m, 'device', side_effect=lambda path: real(path) + 9 if str(path) in guard else real(path))

    def test_D_renumbered_devices_are_accepted_by_select_and_validate_selected(self):
        self.enroll(); want = self.select()
        self.renumber('host', 'profile_root', 'registry_file', 'native_home')
        self.assertEqual(self.select(), want)
        row = json.loads(self.state.read_bytes())['profiles']['ex']
        self.m.validate_selected(row, 'ex', str(self.profile), str(self.consumer))
        # Records are never rewritten by selection.
        before = (self.index.read_bytes(), self.state.read_bytes()); self.select()
        self.assertEqual(before, (self.index.read_bytes(), self.state.read_bytes()))

    def test_D_each_field_alone_is_accepted_and_ino_or_uid_change_is_refused(self):
        self.enroll(); want = self.select(); index = self.index.read_bytes(); state = self.state.read_bytes()
        for field in self.FIELDS:
            with self.subTest(field=field, case='dev'):
                self.renumber(field); self.assertEqual(self.select(), want)
            self.index.write_bytes(index); self.state.write_bytes(state)
            for key in ('ino', 'uid'):
                with self.subTest(field=field, case=key):
                    self.renumber(field, key=key); self.rejected(self.select, 'OMP_BINDING_MISMATCH')
                self.index.write_bytes(index); self.state.write_bytes(state)
            with self.subTest(field=field, case='dev+ino'):
                self.renumber(field); self.renumber(field, key='ino'); self.rejected(self.select, 'OMP_BINDING_MISMATCH')
            self.index.write_bytes(index); self.state.write_bytes(state)

    def test_D_validate_selected_refuses_ino_change_with_renumbered_dev(self):
        self.enroll(); self.renumber('profile_root', 'native_home')
        row = json.loads(self.state.read_bytes())['profiles']['ex']
        for edit in (lambda r: r['profile_binding']['root_identity'].update(ino=r['profile_binding']['root_identity']['ino'] + 1),
                     lambda r: r['native_home']['root_identity'].update(ino=r['native_home']['root_identity']['ino'] + 1)):
            bad = copy.deepcopy(row); edit(bad)
            self.rejected(lambda: self.m.validate_selected(bad, 'ex', str(self.profile), str(self.consumer)), 'OMP_BINDING_MISMATCH')

    def test_D_two_old_devices_for_one_new_device_are_refused(self):
        self.enroll()
        def edit(index, state):
            for ident in self.stored_identities('host', index, state): ident['dev'] += 1
            for ident in self.stored_identities('native_home', index, state): ident['dev'] += 2
        self.rewrite(edit); self.rejected(self.select, 'OMP_BINDING_MISMATCH')

    def test_D_devmap_consistency_unit(self):
        D = self.m.DevMap; a = {'dev': 1, 'ino': 5, 'uid': 7}
        def cur(dev, ino=5): return {'dev': dev, 'ino': ino, 'uid': 7}
        with patch.object(self.m, 'remap_chain', return_value=True):
            m = D(); self.assertTrue(m.match(a, cur(2), '/x')); self.assertTrue(m.match(a, cur(2), '/y'))
            self.assertFalse(m.match(a, cur(3), '/z'))                      # one old -> two new
            m = D(); self.assertTrue(m.match(a, cur(2), '/x'))
            self.assertFalse(m.match(dict(a, dev=4), cur(2), '/y'))         # two old -> one new
            m = D(); self.assertTrue(m.match(a, cur(1), '/x'))              # unchanged device
            self.assertFalse(m.match(a, cur(2), '/y'))                      # same old device also moved
            m = D(); self.assertTrue(m.match(a, cur(2), '/x'))
            self.assertTrue(m.match(dict(a, dev=2), cur(2), '/y'))          # already-current device
            self.assertFalse(D().match(a, cur(2, ino=6), '/x'))
            self.assertFalse(D().match(a, dict(cur(2), gid=1), '/x'))
            self.assertFalse(D().match(dict(a, dev=True), cur(1), '/x'))
            self.assertFalse(D().match([], cur(2), '/x'))
        with patch.object(self.m, 'remap_chain', return_value=False):
            self.assertFalse(D().match(a, cur(2), '/x'))

    def test_D_mount_at_parent_is_refused_per_field(self):
        self.enroll(); want = self.select()
        for field in self.FIELDS:
            self.renumber(field)
            with self.subTest(field=field):
                with self.other_device([self.parent_of(field)]):
                    self.rejected(self.select, 'OMP_BINDING_MISMATCH')
                self.assertEqual(self.select(), want)  # same fixture, no mount: accepted
            self.renumber(field, delta=-1)

    def test_D_mount_between_object_and_anchor_is_refused_even_when_parent_matches(self):
        self.enroll(); self.renumber('host')
        # .config is two levels above the registry directory, below the home anchor.
        with self.other_device([self.registry.parent.parent]):
            self.rejected(self.select, 'OMP_BINDING_MISMATCH')
        with self.other_device([self.home]):
            self.rejected(self.select, 'OMP_BINDING_MISMATCH')


if __name__=='__main__': unittest.main(verbosity=2)
PYTEST
