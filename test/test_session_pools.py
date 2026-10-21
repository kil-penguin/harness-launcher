"""Registered source identity, rather than a workspace spelling, owns pools."""
import subprocess
import sys
import tempfile
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PoolTests(unittest.TestCase):
    def run_case(self, body):
        with tempfile.TemporaryDirectory() as tmp:
            program = '''
from pathlib import Path
import sys
sys.path.insert(0,sys.argv[1])
from harness_session_pools import profile_state, source_pools, find_owner, PoolError
p=Path(sys.argv[2]).resolve();base=p/'session-state';registry=p/'registry';registry.mkdir()
for profile in ('alpha','beta','gamma'):
    root=p/(profile+'-source');(root/'config').mkdir(parents=True)
    (root/'config/launcher.env').write_text('fixture')
    (registry/profile).write_text(str(root)+'\\n')
''' + body
            result = subprocess.run([sys.executable, '-I', '-B', '-c', program,
                                     str(ROOT/'bin'), tmp], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_new_profiles_are_separate_but_discovery_includes_shared_original(self):
        self.run_case('''
assert profile_state(base,p/'gamma-source',registry)==base/'gamma'
assert source_pools(base,p/'gamma-source',registry)==[base/'gamma',base]
assert profile_state(base,p/'alpha-source',registry)==base/'alpha'
assert not base.exists(), 'discovery must not create a pool'
''')

    def test_exact_old_owner_keeps_absolute_path_and_profile_owner_is_discovered(self):
        self.run_case('''
uid='11111111-2222-4333-8444-555555555555'
for pool in (base,base/'gamma'):
    d=pool/'sessions'/uid;d.mkdir(parents=True)
    (d/'source-root').write_text(str(p/'gamma-source')+'\\n')
    (d/'session-root').write_text(str(pool/'worktrees'/uid)+'\\n')
    if pool==base:
        assert find_owner(base,p/'gamma-source',uid,registry)==(base,uid)
try:find_owner(base,p/'gamma-source',uid,registry)
except PoolError:pass
else:raise AssertionError('duplicate owner must refuse')
(base/'sessions'/uid/'source-root').write_text(str(p/'alpha-source')+'\\n')
try:find_owner(base,p/'gamma-source',uid,registry)
except PoolError:pass
else:raise AssertionError('same UUID with foreign source must refuse')
''')

    def test_ambiguous_registry_and_redirected_pool_are_not_authority(self):
        self.run_case('''
(registry/'other').write_text(str(p/'gamma-source')+'\\n')
try:profile_state(base,p/'gamma-source',registry)
except PoolError:pass
else:raise AssertionError('ambiguous source profile accepted')
(registry/'other').unlink();base.mkdir();outside=p/'outside';outside.mkdir();(base/'gamma').symlink_to(outside)
try:profile_state(base,p/'gamma-source',registry)
except PoolError:pass
else:raise AssertionError('symlink pool accepted')
''')

    def test_pool_marker_is_atomic_and_preserves_shared_binding_home(self):
        self.run_case('''
from harness_session_pools import initialize_pool,binding_home
from unittest.mock import patch
with patch('harness_session_pools.os.fsync',side_effect=OSError('fixture publication failure')):
    try:initialize_pool(base,p/'gamma-source',registry)
    except OSError:pass
    else:raise AssertionError('publication failure ignored')
assert not (base/'gamma/profile-state.json').exists(), 'partial marker became authoritative'
pool=initialize_pool(base,p/'gamma-source',registry)
assert initialize_pool(base,p/'gamma-source',registry)==pool
assert binding_home(pool,p/'gamma-source',registry)==base.parent
assert binding_home(base,p/'gamma-source',registry)==base.parent
assert (pool/'profile-state.json').stat().st_mode&0o777==0o600
''')

    def test_managed_headless_uses_registered_pool_before_starting_agent(self):
        self.run_case('''
import os,shutil
from unittest.mock import patch
import harness_headless as headless
home=p/'home';home.mkdir();registration=home/'.config/harness-launcher/profiles'
registration.parent.mkdir(parents=True);shutil.copytree(registry,registration)
prompt=p/'prompt';prompt.write_text('owned fixture prompt')
args=headless.parse_args([str(p/'gamma-source'),'--prompt-file',str(prompt),'--result-file',str(p/'result'),
    '--lock-file',str(p/'lock'),'--budget-usd','1','--timeout-min','1'])
observed={}
def agent(args,caller,source,env,state,*rest):
    observed.update(env=env,state=state,source=source)
    return None
with patch.object(Path,'home',return_value=home),patch.dict(os.environ,{
    'HOME':str(home),'HARNESS_SESSION_LAYOUT':'profiles-v1','HARNESS_SESSION_POOL_BASE':str(base),
    'HARNESS_SESSION_STATE_HOME':str(base)}),patch.object(headless,'harness_dir',return_value=p/'gamma-source'), \
    patch.object(headless,'validate_agent_args',return_value=None),patch.object(headless,'sandbox_preflight'), \
    patch.object(headless,'run_claude',side_effect=agent):
    headless.run(args,{},str(p/'run-tmp'))
assert observed['state']==base/'gamma'
assert observed['env']['HARNESS_SESSION_STATE_HOME']==str(base/'gamma')
assert (base/'gamma/profile-state.json').is_file()
assert not (base/'alpha').exists() and not (base/'sessions').exists()
''')


if __name__ == '__main__':
    unittest.main()
