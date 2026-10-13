"""Native-compatible approved hook trust across fresh isolated homes."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_codex_surface as fixtures

@unittest.skipUnless(Path("/usr/bin/lockf").is_file(), "requires macOS /usr/bin/lockf")
class IsolatedTrustIntegrationTests(unittest.TestCase):
    def build(self, tweak=lambda source, target, record: None, post_prepare=None):
        fixture = fixtures.PrepareIntegrationTests('runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        source = fixture.repo.resolve()
        fixture.repo = source
        fixture.home.joinpath('.codex').mkdir(exist_ok=True)
        global_config=fixture.home/'.codex/config.toml'
        global_config.write_text('# untouched global fixture\n')
        global_before=global_config.read_bytes()
        package = fixture.tmp.resolve()/'launcher-bin'
        shutil.copytree(ROOT/'bin',package,ignore=shutil.ignore_patterns('__pycache__'))
        prepare_patch=mock.patch.object(fixtures,'PREPARE',package/'codex-home-prepare.sh')
        prepare_patch.start();self.addCleanup(prepare_patch.stop)
        script = source / 'core/hooks/session-start.sh'
        settings = {'hooks': {'SessionStart': [{'hooks': [{'type':'command','command':'bash ' + str(script),'timeout':7}]}]}}
        (source / '.claude/settings.json').write_text(json.dumps(settings))
        fixture.prepare(HARNESS_CODEX_SLACK_APPS="")
        home = source / '.harness/codex'
        hook_file = home / 'hooks.json'
        h = json.loads(hook_file.read_text())['hooks']['SessionStart'][0]['hooks'][0]
        # Native 0.161 hooks/list verified normalization for this command subset.
        normalized = {'event_name':'session_start','hooks':[dict(type='command', command=h['command'], timeout=7, **{'async':False})]}
        fingerprint = 'sha256:' + hashlib.sha256(json.dumps(normalized,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        with (home/'config.toml').open('a') as f:
            f.write('\n[hooks.state.'+json.dumps(str(hook_file)+':session_start:0:0')+']\ntrusted_hash = '+json.dumps(fingerprint)+'\nenabled = false\n')
        (source/'.gitignore').write_text('.harness/\n')
        subprocess.run(['git','init','-q',str(source)],check=True)
        subprocess.run(['git','-C',str(source),'add','.'],check=True)
        env=fixture.environment(GIT_AUTHOR_NAME='Fixture',GIT_AUTHOR_EMAIL='fixture@example.invalid',GIT_COMMITTER_NAME='Fixture',GIT_COMMITTER_EMAIL='fixture@example.invalid')
        subprocess.run(['git','-C',str(source),'-c','core.hooksPath=/dev/null','commit','-qm','fixture'],env=env,check=True)
        sid='11111111-1111-4111-8111-111111111111'
        state=fixture.tmp.resolve()/'launcher-state'; target=state/'worktrees'/sid; record=state/'sessions'/sid
        shutil.copytree(source,target,ignore=shutil.ignore_patterns('.harness'))
        record.mkdir(parents=True)
        for name,value in {'source-root':str(source),'session-root':str(target),'base-sha':subprocess.check_output(['git','-C',str(target),'rev-parse','HEAD'],text=True).strip(),'root-inode':f'{target.stat().st_dev} {target.stat().st_ino}','journal':'state=OPEN'}.items():
            (record/name).write_text(value+'\n')
        approvals=state/'hook-approvals';approvals.mkdir()
        grant=approvals/(hashlib.sha256(str(source).encode()).hexdigest()+'.json')
        grant.write_text(json.dumps({'schema_version':1,'source':str(source),'algorithm':'codex-0.161-command-hooks','approval_receipt_sha256':'a'*64,'approved_snapshot':{'base_sha':(record/'base-sha').read_text().strip(),'audit_raw_hooks_sha256':hashlib.sha256(hook_file.read_bytes()).hexdigest()},'hooks':json.loads(hook_file.read_text()),'normalization':'literal-core-and-approved-callback-paths-v1','normalized_hooks_sha256':hashlib.sha256(json.dumps(json.loads(hook_file.read_text()),sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest(),'codex_binary_sha256':hashlib.sha256(fixture.codex_bin.read_bytes()).hexdigest(),'codex_version':'codex-cli 0.153.2','callbacks':{str(package/name):hashlib.sha256((package/name).read_bytes()).hexdigest() for name in ('codex-hook-adapter.sh','codex-pretool-adapter.py','codex-cmux-title-sync.py','harness-launch-record')},'scripts':{'core/hooks/session-start.sh':{'sha256':hashlib.sha256(script.read_bytes()).hexdigest(),'base_blob_sha256':hashlib.sha256(script.read_bytes()).hexdigest()}}}))
        grant.chmod(0o600)
        tweak(source, target, record)
        fixture.repo=target
        fixture.prepare(HARNESS_CODEX_SLACK_APPS="",HARNESS_SOURCE_ROOT=str(source),HARNESS_SESSION_ROOT=str(target),HARNESS_SESSION_ID=sid,HARNESS_SESSION_STATE_HOME=str(state))
        config=tomllib.loads((target/'.harness/codex/config.toml').read_text())
        key=str(target/'.harness/codex/hooks.json')+':session_start:0:0'
        if post_prepare:
            post_prepare(fixture, source, target, record)
        first=(target/'.harness/codex/config.toml').read_bytes()
        overlay_before=(target/'.harness/codex/rich.config.toml').read_bytes()
        fixture.prepare(HARNESS_CODEX_SLACK_APPS="",HARNESS_SOURCE_ROOT=str(source),HARNESS_SESSION_ROOT=str(target),HARNESS_SESSION_ID=sid,HARNESS_SESSION_STATE_HOME=str(state))
        self.assertEqual(first,(target/'.harness/codex/config.toml').read_bytes(),'warm prep changed runtime trust state')
        self.assertEqual(overlay_before,(target/'.harness/codex/rich.config.toml').read_bytes(),'profile hook approval was erased by preparation')
        self.assertEqual(global_before,global_config.read_bytes(),'global config changed')
        return config, key

    def test_fresh_isolated_home_inherits_only_approved_unchanged_hook(self):
        config, key = self.build()
        self.assertIn(key,config.get('hooks',{}).get('state',{}),'fresh isolated home lost an already approved unchanged hook')
        self.assertFalse(config['hooks']['state'][key]['enabled'])
        self.assertEqual(len(config['hooks']['state']),1,'unapproved launcher hooks must stay untrusted')

    def test_changed_or_unapproved_evidence_does_not_gain_trust(self):
        def stale(source, target, record):
            p=source/'.harness/codex/config.toml'
            p.write_text(p.read_text().replace('trusted_hash = "sha256:', 'trusted_hash = "sha256:bad'))
        def changed_script(source, target, record):
            (target/'core/hooks/session-start.sh').write_text('#!/bin/sh\nexit 42\n')
        def forged_record(source, target, record):
            (record/'source-root').write_text(str(target)+'\n')
        def wrong_inode(source, target, record):
            (record/'root-inode').write_text('0 0\n')
        def missing_approval(source, target, record):
            (source/'.harness/codex/config.toml').write_text('model="fixture"\n')
        def changed_matcher(source, target, record):
            p=target/'.claude/settings.json'; d=json.loads(p.read_text());d['hooks']['SessionStart'][0]['matcher']='changed';p.write_text(json.dumps(d))
        def stale_open_record(source, target, record):
            (record/'journal').write_text('state=OPEN\nstate=DELIVERED\n')
        def no_grant(source, target, record):
            next((record.parents[1]/'hook-approvals').glob('*.json')).unlink()
        def wrong_grant_source(source, target, record):
            p=next((record.parents[1]/'hook-approvals').glob('*.json'));d=json.loads(p.read_text());d['source']=str(target);p.write_text(json.dumps(d))
        def wrong_grant_mode(source, target, record):
            next((record.parents[1]/'hook-approvals').glob('*.json')).chmod(0o644)
        def changed_grant_set(source, target, record):
            p=next((record.parents[1]/'hook-approvals').glob('*.json'));d=json.loads(p.read_text());d['hooks']['hooks']['SessionStart'].reverse();p.write_text(json.dumps(d))
        def native_drift(source, target, record):
            p=next((record.parents[1]/'hook-approvals').glob('*.json'));d=json.loads(p.read_text());d['codex_binary_sha256']='0'*64;p.write_text(json.dumps(d))
        def wrong_grant_base(source, target, record):
            p=next((record.parents[1]/'hook-approvals').glob('*.json'));d=json.loads(p.read_text());d['approved_snapshot']['base_sha']='0'*40;p.write_text(json.dumps(d))
        def wrong_base_blob(source,target,record):
            p=next((record.parents[1]/'hook-approvals').glob('*.json'));d=json.loads(p.read_text());d['scripts']['core/hooks/session-start.sh']['base_blob_sha256']='0'*64;p.write_text(json.dumps(d))
        def symlink_script(source, target, record):
            p=target/'core/hooks/session-start.sh';p.unlink();p.symlink_to(source/'core/hooks/session-start.sh')
        for tweak in (stale, changed_script, forged_record, wrong_inode, missing_approval, changed_matcher, symlink_script, stale_open_record, no_grant, wrong_grant_source, wrong_grant_mode, changed_grant_set, native_drift, wrong_grant_base, wrong_base_blob):
            with self.subTest(case=tweak.__name__):
                config,key=self.build(tweak)
                self.assertNotIn(key,config.get('hooks',{}).get('state',{}))

    def test_normalized_grant_digest_is_verified(self):
        def bad_digest(source,target,record):
            p=next((record.parents[1]/'hook-approvals').glob('*.json'));d=json.loads(p.read_text());d['normalized_hooks_sha256']='0'*64;p.write_text(json.dumps(d))
        config,key=self.build(bad_digest)
        self.assertNotIn(key,config.get('hooks',{}).get('state',{}))

    def test_unrelated_new_snapshot_preserves_approved_hooks(self):
        def unrelated_commit(source,target,record):
            (target/'unrelated.txt').write_text('unrelated metadata')
            subprocess.run(['git','-C',str(target),'add','unrelated.txt'],check=True)
            subprocess.run(['git','-C',str(target),'-c','core.hooksPath=/dev/null','-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-qm','unrelated'],check=True)
            (record/'base-sha').write_bytes(subprocess.check_output(['git','-C',str(target),'rev-parse','HEAD']))
        config,key=self.build(unrelated_commit)
        self.assertIn(key,config.get('hooks',{}).get('state',{}))

    def test_identical_callback_in_new_verified_package_path_inherits(self):
        def new_package(source,target,record):
            parent=record.parents[1].parent
            original=parent/'launcher-bin';current=parent/'current-runtime-bin'
            shutil.copytree(original,current)
            switch=mock.patch.object(fixtures,'PREPARE',current/'codex-home-prepare.sh');switch.start();self.addCleanup(switch.stop)
        config,key=self.build(new_package)
        self.assertIn(key,config.get('hooks',{}).get('state',{}))

    def test_modified_external_callback_does_not_gain_trust(self):
        def changed_external(source, target, record):
            package=record.parents[1].parent/'launcher-bin'
            (package/'codex-hook-adapter.sh').write_text('#!/bin/sh\nexit 42\n')
        config,key=self.build(changed_external)
        self.assertNotIn(key,config.get('hooks',{}).get('state',{}))

    def test_existing_target_and_foreign_decisions_are_preserved(self):
        def decisions(source, target, record):
            home=target/'.harness/codex';home.mkdir(parents=True)
            key=str(home/'hooks.json')+':session_start:0:0'
            text='[hooks.state.'+json.dumps(key)+']\ntrusted_hash="sha256:user-decision"\nenabled=false # keep exact\n\n[hooks.state."foreign@plugin:source:stop:0:0"]\ntrusted_hash="sha256:foreign"\nenabled=false # foreign exact\n'
            (home/'config.toml').write_text(text)
        config,key=self.build(decisions)
        self.assertEqual(config['hooks']['state'][key]['trusted_hash'],'sha256:user-decision')
        self.assertFalse(config['hooks']['state']['foreign@plugin:source:stop:0:0']['enabled'])

    def test_native_profile_approval_survives_preparation(self):
        def approved_profile(fixture, source, target, record):
            p=target/'.harness/codex/rich.config.toml'
            key=str(target/'.harness/codex/hooks.json')+':session_start:0:0'
            with p.open('a') as f:
                for i in range(48):
                    selected=key if i==0 else f'foreign@plugin:source:stop:{i}:0'
                    f.write('\n[hooks.state.'+json.dumps(selected)+']\ntrusted_hash="sha256:profile-user-decision"\nenabled=false # exact profile choice\n')
            before=p.read_bytes(); calls=fixture.counter.read_bytes()
            env=dict(HARNESS_CODEX_SLACK_APPS='',HARNESS_SOURCE_ROOT=str(source),HARNESS_SESSION_ROOT=str(target),HARNESS_SESSION_ID=record.name,HARNESS_SESSION_STATE_HOME=str(record.parents[1]))
            fixture.prepare(**env)
            self.assertEqual(calls,fixture.counter.read_bytes(),'native profile trust unnecessarily invalidated warm state')
            self.assertEqual(before,p.read_bytes())
            manifest=target/'config/codex-surface.json';d=json.loads(manifest.read_text());d['repo']='changed';manifest.write_text(json.dumps(d))
            fixture.prepare(**env)
            self.assertEqual(before,p.read_bytes(),'cold prep erased native/foreign profile approvals')
            p.write_bytes(b'unknown_profile_key=true\n'+before)
            fixture.prepare(**env)
            self.assertEqual(before,p.read_bytes(),'repair of unmanaged profile drift lost hook decisions')
            self.assertEqual(len(tomllib.loads(p.read_text())['hooks']['state']),48)
        self.build(post_prepare=approved_profile)

    def test_malformed_native_profile_repairs_without_inheriting_bad_state(self):
        def malformed_profile(fixture, source, target, record):
            p = target / '.harness/codex/rich.config.toml'
            env = dict(HARNESS_CODEX_SLACK_APPS='', HARNESS_SOURCE_ROOT=str(source),
                       HARNESS_SESSION_ROOT=str(target), HARNESS_SESSION_ID=record.name,
                       HARNESS_SESSION_STATE_HOME=str(record.parents[1]))
            for content in (b'[hooks.state.\"broken\"\ntrusted_hash=\"unverified\"\n', b'\xff\xfeinvalid'):
                with self.subTest(content=content):
                    p.write_bytes(content)
                    fixture.prepare(**env)
                    repaired = tomllib.loads(p.read_text())
                    self.assertNotIn('state', repaired.get('hooks', {}))
                    self.assertEqual(repaired['model'], 'gpt-6.1-sol')
        self.build(post_prepare=malformed_profile)

    def test_native_0161_current_hash_fixture(self):
        spec=importlib.util.spec_from_file_location('codex_hook_trust_test',ROOT/'bin/codex-hook-trust.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        command='/bin/sh -c \'s="$HOME/.orca/agent-hooks/codex-hook.sh"; [ -x "$s" ] && { /bin/sh "$s" >/dev/null 2>&1; exit 0; }; cat >/dev/null\''
        handler={'type':'command','command':command,'timeout':5}
        self.assertEqual(module.fingerprint('Stop',None,handler),'sha256:e15f2c7d58c59e58ca47fd189969fe9041eaed2f185bb5e4e8d43b24c1f01427')

class ApprovalFileSafetyTests(unittest.TestCase):
    def test_public_writable_and_hardlinked_files_are_rejected(self):
        spec=importlib.util.spec_from_file_location('approval_reader',ROOT/'bin/codex-hook-trust.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();p=root/'evidence';p.write_bytes(b'approved');p.chmod(0o666)
            with self.assertRaises((OSError,ValueError)):m.regular(p,root)
            p.chmod(0o644);os.link(p,root/'alias')
            with self.assertRaises((OSError,ValueError)):m.regular(p,root)

    def test_symlink_swap_during_open_is_rejected(self):
        spec=importlib.util.spec_from_file_location('approval_reader_swap',ROOT/'bin/codex-hook-trust.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();p=root/'evidence';p.write_bytes(b'approved');other=root/'other';other.write_bytes(b'foreign')
            original_open=os.open
            def swapped(path, flags, *args, **kwargs):
                if str(path)=='evidence':p.unlink();p.symlink_to(other)
                return original_open(path,flags,*args,**kwargs)
            with mock.patch.object(m.os,'open',side_effect=swapped):
                with self.assertRaises((OSError,ValueError)):m.regular(p,root)

class CallbackMappingSafetyTests(unittest.TestCase):
    def test_mapping_is_limited_to_verified_package_token_and_unique_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();current=root/'current';old=root/'old';other=root/'other'
            for d in (current,old,other):d.mkdir()
            name='codex-cmux-title-sync.py'
            for d in (current,old,other):(d/name).write_bytes(b'approved callback')
            helper=current/'codex-hook-trust.py';helper.write_bytes((ROOT/'bin/codex-hook-trust.py').read_bytes())
            spec=importlib.util.spec_from_file_location('mapping_fixture',helper);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
            approved=hashlib.sha256((old/name).read_bytes()).hexdigest();grant={'callbacks':{str(old/name):approved}}
            command='python3 '+str(current/name)
            self.assertEqual(m.approved_package_command(command,grant),'python3 '+str(old/name))
            with self.assertRaises((OSError,ValueError)):m.approved_package_command('python3 '+str(other/name),grant)
            with self.assertRaises((OSError,ValueError)):m.approved_package_command('python3 "'+str(current/name)+'"',grant)
            with self.assertRaises((OSError,ValueError)):m.approved_package_command(command+' extra',grant)
            duplicate={'callbacks':{str(old/name):approved,str(other/name):approved}}
            with self.assertRaises((OSError,ValueError)):m.approved_package_command(command,duplicate)
            (current/name).write_bytes(b'changed callback')
            with self.assertRaises((OSError,ValueError)):m.approved_package_command(command,grant)

    def test_launch_record_callback_shape_is_accepted_only_exactly(self):
        spec=importlib.util.spec_from_file_location('callback_shape',ROOT/'bin/codex-hook-trust.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
        path=ROOT/'bin/harness-launch-record'
        grant={'callbacks':{str(path):hashlib.sha256(path.read_bytes()).hexdigest()}}
        for tail in ('codex','codex --callback'):
            command='python3 '+str(path)+' '+tail
            self.assertEqual(m.approved_package_command(command,grant),command)
        for tail in ('codex --callback extra','codex --other','claude --callback','--callback'):
            with self.assertRaises((OSError,ValueError)):
                m.approved_package_command('python3 '+str(path)+' '+tail,grant)

if __name__ == '__main__': unittest.main()
