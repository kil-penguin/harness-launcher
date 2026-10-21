"""Only owned fixtures: targeted GC must never retire unaccounted work."""
import json
import fcntl
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
ENGINE=ROOT/'bin/session-isolation.sh'


class GCSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name).resolve();self.source=self.base/'source';self.source.mkdir()
        self.state=self.base/'state';self.env=dict(os.environ,HARNESS_SESSION_STATE_HOME=str(self.state),
                                                  HARNESS_SESSION_RETENTION_SECONDS='0',HARNESS_AGENT_USAGE_TEST_MODE='1')
        for args in [('init','-q','-b','main'),('config','user.name','Fixture'),('config','user.email','qa@example.invalid')]:
            subprocess.run(['git','-C',str(self.source),*args],check=True)
        (self.source/'tracked').write_text('fixture\n')
        subprocess.run(['git','-C',str(self.source),'add','.'],check=True)
        subprocess.run(['git','-C',str(self.source),'commit','-qm','fixture'],check=True)

    def engine(self,*args):
        done=subprocess.run([str(ENGINE),*args],env=self.env,capture_output=True,text=True,timeout=60)
        self.assertEqual(done.returncode,0,done.stderr)
        return done

    def closed(self):
        fields=dict(line.split('=',1) for line in self.engine('create',str(self.source)).stdout.splitlines())
        owner=fields['HARNESS_SESSION_ID'];root=Path(fields['HARNESS_SESSION_ROOT'])
        self.engine('exit',owner)
        return owner,root

    def test_targeted_pass_retires_only_requested_clean_owner(self):
        owner,root=self.closed();other,other_root=self.closed()
        self.engine('gc','--owner',owner,'--source-root',str(self.source))
        self.assertFalse(root.exists());self.assertTrue(other_root.exists())
        self.assertTrue((self.state/'archives'/owner/'manifest.json').is_file())

    def test_existing_absolute_state_alias_keeps_its_recorded_spelling(self):
        # macOS TemporaryDirectory spells /var while pwd -P spells /private/var.
        self.env['HARNESS_SESSION_STATE_HOME']=str(Path(self.tmp.name)/'state')
        owner,root=self.closed()
        recorded=(self.state/'sessions'/owner/'session-root').read_text()
        self.engine('gc','--owner',owner,'--source-root',str(self.source))
        self.assertFalse(root.exists())
        self.assertEqual((self.state/'sessions'/owner/'session-root').read_text(),recorded)

    def test_work_added_after_terminal_transition_survives_gc(self):
        owner,root=self.closed();(root/'tracked').write_text('only copy of later work\n')
        self.engine('gc')
        self.assertEqual((root/'tracked').read_text(),'only copy of later work\n')

    def test_normal_exit_records_observed_reason_without_reclassifying_old_abandoned(self):
        owner,root=self.closed();record=self.state/'sessions'/owner
        self.assertIn('reason=clean',(record/'exit-reason').read_text())

    def test_generated_codex_home_without_native_payload_needs_no_native_probe(self):
        owner,root=self.closed();home=root/'.harness/codex';home.mkdir(parents=True)
        (home/'config.toml').write_text('model = "generated-fixture"\n')
        self.env['HARNESS_CODEX_BIN']=str(self.base/'unavailable-native')
        self.engine('gc','--owner',owner,'--source-root',str(self.source))
        self.assertFalse(root.exists())
        self.assertTrue((self.state/'archives'/owner/'manifest.json').is_file())

    def test_unknown_codex_artifact_is_retained(self):
        owner,root=self.closed();home=root/'.harness/codex';home.mkdir(parents=True)
        (home/'unknown-user-data').write_text('unique fixture data')
        self.engine('gc','--owner',owner,'--source-root',str(self.source))
        self.assertEqual((home/'unknown-user-data').read_text(),'unique fixture data')

    def test_stale_list_does_not_abandon_a_live_runtime_lease(self):
        fields=dict(line.split('=',1) for line in self.engine('create',str(self.source)).stdout.splitlines())
        owner=fields['HARNESS_SESSION_ID'];record=self.state/'sessions'/owner
        (record/'heartbeat').write_text('2000-01-01T00:00:00Z\n')
        with (record/'runtime.lock').open('r+') as lease:
            fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.assertIn(owner+' OPEN',self.engine('list').stdout)
        self.assertIn(owner+' ABANDONED',self.engine('list').stdout)
        self.assertIn('reason=stale_without_lease',(record/'exit-reason').read_text())


if __name__=='__main__':unittest.main()
