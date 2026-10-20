"""Old immutable headless drops policy; the managed new entry must replace it."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
BASELINE='73bec819f8c837d83446b00c3bd692a1065d2596'


def run(args,**kwargs):
    return subprocess.run(args,check=True,capture_output=True,text=True,**kwargs).stdout


@unittest.skipUnless(Path('/usr/bin/lockf').is_file(),'macOS kernel lock required')
class CompatibilityTests(unittest.TestCase):
    def test_old_dropped_policy_deletes_unbacked_raw_but_new_entry_archives_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            base=Path(temporary).resolve();old=base/'old';old.mkdir()
            for name in ('session-isolation.sh','harness_headless.py','harness_profile_resolver.py','harness_target.py'):
                try:data=run(['git','-C',str(ROOT),'show',BASELINE+':bin/'+name])
                except subprocess.CalledProcessError:self.skipTest('immutable 0.50.1 baseline object unavailable')
                path=old/name;path.write_text(data);path.chmod(0o700)
            specification=importlib.util.spec_from_file_location('owned_old_headless',old/'harness_headless.py')
            legacy=importlib.util.module_from_spec(specification);specification.loader.exec_module(legacy)
            specification=importlib.util.spec_from_file_location('owned_new_headless',ROOT/'bin/harness_headless.py')
            current=importlib.util.module_from_spec(specification);specification.loader.exec_module(current)
            source=base/'source';source.mkdir();(source/'config').mkdir()
            (source/'config/launcher.env').write_text('HARNESS_PREFIX=fixture\nHARNESS_NAME=Fixture\n')
            (source/'.gitignore').write_text('.harness/\n')
            run(['git','-C',str(source),'init','-b','main'])
            run(['git','-C',str(source),'add','.'])
            run(['git','-C',str(source),'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-m','fixture'])
            remote=base/'remote.git';run(['git','init','--bare',str(remote)])
            run(['git','-C',str(source),'remote','add','origin',str(remote)])
            run(['git','-C',str(source),'push','-u','origin','main'])
            home=base/'home';home.mkdir(mode=0o700);state=base/'state';state.mkdir(mode=0o700)
            env={'HOME':str(home),'PATH':os.environ['PATH'],'HARNESS_SESSION_STATE_HOME':str(state),'HARNESS_PYTHON_BIN':sys.executable,'HARNESS_SESSION_RETENTION_SECONDS':'archive'}
            with patch.dict(os.environ,env,clear=True):
                old_env=legacy.child_env(str(base/'old-tmp'))
                new_env=current.child_env(str(base/'new-tmp'))
            self.assertNotIn('HARNESS_SESSION_RETENTION_SECONDS',old_env)
            self.assertEqual(new_env['HARNESS_SESSION_RETENTION_SECONDS'],'archive')
            native=base/'native-unavailable';native.write_text('#!/bin/sh\nexit 1\n');native.chmod(0o700)
            # Broker GC gets the owned rejecting binary explicitly; child_env
            # intentionally does not forward arbitrary executable overrides.
            new_env=dict(new_env,HARNESS_CODEX_BIN=str(native))
            for tag in ('old','new'):
                fields=dict(line.split('=',1) for line in run([str(ROOT/'bin/session-isolation.sh'),'create',str(source)],env=env).splitlines() if '=' in line)
                owner=fields['HARNESS_SESSION_ID'];work=Path(fields['HARNESS_SESSION_ROOT'])
                transcript=work/'.harness/codex/sessions/2026/10/10/rollout-fixture-aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee.jsonl'
                transcript.parent.mkdir(parents=True)
                transcript.write_text(json.dumps({'type':'session_meta','payload':{'id':'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee','cwd':str(work)}})+'\n'+
                                      json.dumps({'type':'response_item','payload':{'type':'message','role':'user','content':[{'type':'input_text','text':'synthetic conversation retained exactly'}]}})+'\n')
                original=transcript.read_bytes()
                run([str(ROOT/'bin/session-isolation.sh'),'close',owner],env=env)
                journal=state/'sessions'/owner/'journal'
                journal.write_text('\n'.join('heartbeat=2000-01-01T00:00:00Z' if line.startswith('heartbeat=') else line for line in journal.read_text().splitlines())+'\n')
                if tag=='old':
                    refused=subprocess.run([str(old/'session-isolation.sh'),'gc'],env=env,capture_output=True)
                    self.assertNotEqual(refused.returncode,0);self.assertTrue(work.exists())
                    run([str(old/'session-isolation.sh'),'gc'],env=old_env)
                    self.assertFalse(work.exists());self.assertFalse((state/'archives'/owner).exists())
                else:
                    run([str(ROOT/'bin/session-isolation.sh'),'gc'],env=new_env)
                    # Transcript backup alone cannot authorize Native root deletion.
                    self.assertTrue(work.exists())
                    self.assertEqual(transcript.read_bytes(),original)
                    self.assertEqual((state/'archives'/owner/'codex'/transcript.relative_to(work/'.harness/codex')).read_bytes(),original)


if __name__=='__main__':unittest.main()
