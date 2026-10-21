"""Real local Git/broker fixtures for durable acceptance and delivery ownership."""
import concurrent.futures
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / 'bin/session-isolation.sh'


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='integration-queue-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.home = self.base/'home'; self.home.mkdir()
        self.queue = self.base/'queue'
        self.state = self.base/'state'
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(('HARNESS_', 'GIT_', 'TEST_HARNESS_'))}
        self.env.update(HOME=str(self.home), XDG_CONFIG_HOME=str(self.home/'.config'),
                        HARNESS_SESSION_STATE_HOME=str(self.state),
                        GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_NOSYSTEM='1',
                        GIT_AUTHOR_NAME='fixture', GIT_AUTHOR_EMAIL='fixture@invalid',
                        GIT_COMMITTER_NAME='fixture', GIT_COMMITTER_EMAIL='fixture@invalid',
                        PYTHONDONTWRITEBYTECODE='1', HARNESS_CLAUDE_PLUGIN_MIRROR='0',
                        HARNESS_AGENT_USAGE_TEST_MODE='1',
                        HARNESS_AGENT_USAGE_STATE_DIR=str(self.base/'usage'))
        self.children = []
        self.addCleanup(self.stop_children)
        self.source, self.remote = self.repository('source')

    def stop_children(self):
        (self.base/'release').touch()
        for child in reversed(self.children):
            if child.poll() is None:
                child.terminate()
                try: child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL); child.wait(timeout=10)
            if child.stdout: child.stdout.close()
            if child.stderr: child.stderr.close()

    def run_command(self, args, *, env=None, check=True, timeout=30, **options):
        child=subprocess.Popen([str(x) for x in args],env=env or self.env,
                               stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,
                               start_new_session=True,**options)
        try:
            stdout,stderr=child.communicate(timeout=timeout)
            result=subprocess.CompletedProcess(args,child.returncode,stdout,stderr)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid,signal.SIGKILL);child.communicate(timeout=5)
            self.fail('owned fixture command exceeded its bounded deadline: '+str(args[1:3]))
        if check:
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return result

    def cli(self, *args, **kwargs):
        return self.run_command([ENGINE, *args], **kwargs)

    def repository(self, name):
        source=self.base/name; remote=self.base/(name+'.git')
        self.run_command(['git','init','-q','-b','main',source])
        (source/'core/bin').mkdir(parents=True)
        verifier=source/'core/bin/auto-deliver.sh'
        verifier.write_text('''#!/bin/bash
set -eu
[[ "${1:-}" != --help ]] || exit 0
if [[ -f block ]]; then
  echo verified >> "''' + str(self.base/'verifications') + '''"
  if [[ -f ignore-term ]]; then trap '' TERM; fi
  touch "''' + str(self.base/'verifier-started') + '''"
  while [[ ! -f "''' + str(self.base/'release') + '''" ]]; do sleep 0.05; done
fi
if [[ -f reject ]]; then exit 1; fi
if [[ -f check-budget ]]; then
  [[ "${HARNESS_DELIVER_TEST_JOBS:-missing}" == "$(cat check-budget)" ]] || exit 1
fi
if [[ -f check-no-authority ]]; then
  [[ -z "${HARNESS_INTEGRATION_JOB:-}${HARNESS_INTEGRATION_LEASES:-}${INTEGRATION_INPUT_DIR:-}" ]] || exit 1
  python3 - "''' + str(self.state/'integration.lock') + '''" <<'CHECK'
import os,sys
target=os.stat(sys.argv[1])
for fd in range(3,512):
    try: info=os.fstat(fd)
    except OSError: continue
    assert (info.st_dev,info.st_ino)!=(target.st_dev,target.st_ino),'verifier inherited broker gate'
CHECK
fi
exit 0
''')
        verifier.chmod(0o755)
        (source/'seed').write_text('seed')
        self.run_command(['git','-C',source,'add','.'])
        self.run_command(['git','-C',source,'commit','-qm','initial'])
        self.run_command(['git','init','--bare','-q',remote])
        self.run_command(['git','--git-dir='+str(remote),'symbolic-ref','HEAD','refs/heads/main'])
        self.run_command(['git','-C',source,'remote','add','origin',remote])
        self.run_command(['git','-C',source,'push','-q','-u','origin','main'])
        return source,remote

    def enable(self, *, state=None, jobs=2):
        env=dict(self.env,HARNESS_SESSION_STATE_HOME=str(state or self.state))
        self.cli('queue','enable','--home',self.queue,'--jobs',jobs,'--test-jobs','1',env=env)
        return env

    def create(self, changes=None, *, source=None, env=None):
        result=self.cli('create',source or self.source,env=env)
        fields=dict(row.split('=',1) for row in result.stdout.splitlines() if '=' in row)
        root=Path(fields['HARNESS_SESSION_ROOT'])
        for name,value in (changes or {'work':'accepted'}).items(): (root/name).write_text(value)
        return fields['HARNESS_SESSION_ID'],root

    def enqueue(self, owner, *, env=None):
        result=self.cli('close','--enqueue',owner,env=env)
        data=json.loads(result.stdout)
        self.assertTrue(data['accepted'])
        self.assertNotEqual(data['state'],'DELIVERED')
        return data['job_id']

    def status(self, job):
        return json.loads(self.cli('queue','status',job,'--home',self.queue).stdout)

    def start_worker(self, *extra):
        child=subprocess.Popen([sys.executable,'-I','-B',str(ROOT/'bin/harness_integration_queue.py'),
                               '--state',str(self.state),'--engine',str(ENGINE),
                               'serve','--home',str(self.queue),*extra],
                               env=self.env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                               text=True,start_new_session=True)
        self.children.append(child)
        return child

    def until(self, predicate, timeout=20):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            if predicate(): return
            time.sleep(.04)
        self.fail('bounded fixture condition did not become true')

    def drain(self):
        self.cli('queue','serve','--home',self.queue,'--once',timeout=45)

    def owned_broker(self, worker, owner):
        rows=self.run_command(['ps','-axo','pid=,ppid=,args=']).stdout.splitlines()
        found=[int(parts[0]) for line in rows if len(parts:=line.split(None,2))==3
               and parts[1]==str(worker.pid) and '_queue-execute integrate '+owner in parts[2]]
        self.assertEqual(len(found),1)
        return found[0]

    def test_acceptance_survives_caller_exit_and_only_worker_delivers(self):
        # Break caught: returning success without a durable, executable job.
        self.enable(); owner,root=self.create()
        job=self.enqueue(owner)
        self.assertEqual(self.status(job)['state'],'queued')
        self.assertNotEqual(self.run_command(['git','--git-dir='+str(self.remote),
                         'cat-file','-e','main:work'],check=False).returncode,0)
        self.drain()
        self.assertEqual(self.status(job)['state'],'delivered')
        observed=self.status(job)
        self.assertIsInstance(observed['sequence'],int)
        self.assertIsInstance(observed['worker_pid'],int)
        self.assertIsInstance(observed['coordinator_pid'],int)
        self.assertEqual(self.run_command(['git','--git-dir='+str(self.remote),
                         'show','main:work']).stdout,'accepted')
        self.assertTrue(root.is_dir())

    def test_repeated_enqueue_returns_one_job_and_keeps_commit_message(self):
        # Break caught: duplicate registration or message ownership stays with caller.
        self.enable(); owner,_=self.create()
        record=self.state/'sessions'/owner
        (record/'commit-message').write_text('fix: preserve accepted message\n')
        first=self.enqueue(owner); second=self.enqueue(owner)
        self.assertEqual(first,second)
        (record/'commit-message').unlink()
        self.drain()
        message=self.run_command(['git','--git-dir='+str(self.remote),'log','-1','--format=%B']).stdout
        self.assertIn('fix: preserve accepted message',message)
        self.assertIn('Harness-Session: '+owner,message)
        self.assertEqual(self.run_command(['git','--git-dir='+str(self.remote),
                         'rev-list','--count','main']).stdout.strip(),'2')
        duplicate=json.loads(self.cli('close','--enqueue',owner).stdout)
        self.assertEqual(duplicate['job_id'],first)
        self.assertEqual(duplicate['state'],'delivered')

    def test_wait_timeout_does_not_cancel_accepted_job(self):
        # Break caught: a client waiting deadline deletes or fails a queued job.
        self.enable(); owner,_=self.create(); job=self.enqueue(owner)
        result=self.cli('wait',job,'--timeout','0.1',check=False)
        self.assertEqual(result.returncode,124,result.stderr)
        self.assertEqual(self.status(job)['state'],'queued')
        self.drain()
        self.cli('wait',job,'--timeout','1')

    def test_repeated_enqueue_during_execution_returns_existing_receipt(self):
        # A lost caller receipt must be recoverable while the worker owns its lease.
        self.enable(); owner,_=self.create({'block':'yes'}); job=self.enqueue(owner)
        worker=self.start_worker();self.until(lambda:(self.base/'verifier-started').exists())
        duplicate=json.loads(self.cli('close','--enqueue',owner).stdout)
        self.assertEqual(duplicate['job_id'],job)
        (self.base/'release').touch()
        self.until(lambda:self.status(job)['state']=='delivered')
        worker.terminate();worker.wait(timeout=10)

    def test_concurrent_intake_cannot_create_two_jobs_or_two_commits(self):
        # Overlapping first intake either returns the receipt or explicitly
        # refuses the busy owner; it must never accept a second execution.
        self.enable();owner,_=self.create();self.cli('submit',owner)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as callers:
            results=list(callers.map(lambda _:self.cli('close','--enqueue',owner,check=False),range(4)))
        receipts=[]
        for result in results:
            if result.returncode==0:receipts.append(json.loads(result.stdout)['job_id'])
            else:
                self.assertEqual(result.returncode,2,result.stderr)
                self.assertIn('already owned',result.stderr)
        self.assertTrue(receipts)
        self.assertEqual(len(set(receipts)),1)
        self.assertEqual(self.enqueue(owner),receipts[0])
        self.drain()
        self.assertEqual(self.status(receipts[0])['state'],'delivered')
        self.assertEqual(self.run_command(['git','--git-dir='+str(self.remote),'rev-list','--count','main']).stdout.strip(),'2')

    def test_unrelated_repository_progresses_while_same_destination_is_fifo(self):
        # Break caught: one global worker/lock blocks unrelated destinations.
        self.enable(jobs=2)
        first,_=self.create({'block':'yes'}); first_job=self.enqueue(first)
        worker=self.start_worker()
        self.until(lambda:(self.base/'verifier-started').exists())
        same,_=self.create({'later':'same repository'}); same_job=self.enqueue(same)
        other_source,other_remote=self.repository('other')
        other,_=self.create({'independent':'yes'},source=other_source)
        other_job=self.enqueue(other)
        self.until(lambda:self.status(other_job)['state']=='delivered')
        self.assertEqual(self.status(same_job)['state'],'queued')
        self.assertEqual(self.status(first_job)['state'],'running')
        self.assertEqual(self.run_command(['git','--git-dir='+str(other_remote),
                         'show','main:independent']).stdout,'yes')
        (self.base/'release').touch()
        self.until(lambda:self.status(same_job)['state']=='delivered')
        worker.terminate();worker.wait(timeout=10)

    def test_legacy_lock_blocks_worker_but_not_durable_acceptance(self):
        # Break caught: new worker bypasses an old runtime still integrating.
        import fcntl
        self.enable(); owner,_=self.create(); job=self.enqueue(owner)
        with (self.state/'integration.lock').open('a') as held:
            fcntl.flock(held,fcntl.LOCK_EX)
            self.drain()
            self.assertEqual(self.status(job)['state'],'queued')
        self.drain()
        self.assertEqual(self.status(job)['state'],'delivered')

    def test_verifier_failure_stops_job_and_unrelated_work_continues(self):
        # Break caught: retrying failed verification indefinitely or blocking all work.
        self.enable(); owner,_=self.create({'reject':'yes'}); bad=self.enqueue(owner)
        other_source,_=self.repository('other')
        other,_=self.create(source=other_source); good=self.enqueue(other)
        self.drain()
        self.assertEqual(self.status(bad)['state'],'failed')
        self.assertEqual(self.status(good)['state'],'delivered')

    def test_snapshot_tampering_fails_without_publishing(self):
        # Break caught: replaying an accepted job from mutable/tampered inputs.
        self.enable(); owner,_=self.create(); job=self.enqueue(owner)
        patch=self.queue/'jobs'/job/'input/submission.patch'
        patch.chmod(0o600);patch.write_text('replaced')
        self.drain()
        self.assertEqual(self.status(job)['state'],'failed')
        self.assertEqual(self.run_command(['git','--git-dir='+str(self.remote),
                         'rev-list','--count','main']).stdout.strip(),'1')

    def test_service_description_is_pinned_and_does_not_install_itself(self):
        # Break caught: a service follows mutable source/PATH, or mere inspection installs it.
        import plistlib
        self.enable()
        data=plistlib.loads(self.cli('queue','service-plist','--home',self.queue).stdout.encode())
        self.assertTrue(Path(data['ProgramArguments'][0]).is_absolute())
        self.assertIn(str(self.queue/'runtimes'),data['ProgramArguments'][2])
        self.assertTrue(data['RunAtLoad'])
        self.assertFalse((self.home/'Library/LaunchAgents').exists())

    def test_explicit_service_install_writes_only_owned_plist_without_starting_worker(self):
        # Break caught: service install is missing, changes another job, or starts work implicitly.
        import plistlib
        self.enable(); owner,_=self.create(); job=self.enqueue(owner)
        result=json.loads(self.cli('queue','service-install','--home',self.queue).stdout)
        target=Path(result['plist'])
        self.assertEqual(target.parent,self.home/'Library/LaunchAgents')
        self.assertEqual(target.stat().st_mode&0o777,0o600)
        self.assertTrue(plistlib.loads(target.read_bytes())['KeepAlive'])
        self.assertEqual(self.status(job)['state'],'queued')

    def test_sync_close_waits_on_same_worker_and_headless_receives_test_budget(self):
        # Break caught: default close bypasses the queue or sandbox drops its budget.
        env=dict(self.env,HARNESS_HEADLESS='1',HARNESS_PYTHON_BIN=sys.executable)
        self.enable(jobs=1); worker=self.start_worker()
        owner,_=self.create({'check-budget':'1'},env=env)
        self.cli('close',owner,env=env,timeout=45)
        state=(self.state/'sessions'/owner/'journal').read_text()
        self.assertIn('state=DELIVERED',state)
        worker.terminate();worker.wait(timeout=10)

    def test_existing_and_profile_pools_share_destination_order(self):
        # Break caught: distinct state roots let two jobs for the same remote overlap.
        self.enable(); other_env=self.enable(state=self.base/'profile-state')
        first,_=self.create({'block':'yes'}); first_job=self.enqueue(first)
        worker=self.start_worker()
        self.until(lambda:(self.base/'verifier-started').exists())
        second,_=self.create({'after':'profile'},env=other_env)
        second_job=self.enqueue(second,env=other_env)
        time.sleep(.4)
        self.assertEqual(self.status(second_job)['state'],'queued')
        (self.base/'release').touch()
        self.until(lambda:self.status(second_job)['state']=='delivered')
        self.assertEqual(self.status(first_job)['state'],'delivered')
        worker.terminate();worker.wait(timeout=10)

    def test_coordinator_crash_cannot_release_a_live_childs_execution_gates(self):
        # Break caught: parent exit transfers ownership while its verifier still runs.
        import fcntl
        self.enable(); other_state=self.base/'other-pool'; self.enable(state=other_state)
        owner,_=self.create({'block':'yes'}); job=self.enqueue(owner)
        worker=self.start_worker()
        self.until(lambda:(self.base/'verifier-started').exists())
        worker.kill();worker.wait(timeout=10)
        with (other_state/'integration.lock').open('a') as gate:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(gate,fcntl.LOCK_EX|fcntl.LOCK_NB)
        restarted=self.cli('queue','serve','--home',self.queue,'--once',check=False)
        self.assertEqual(restarted.returncode,2,restarted.stderr)
        (self.base/'release').touch()
        self.until(lambda:'state=DELIVERED' in (self.state/'sessions'/owner/'journal').read_text())
        # The child can finish just after writing its final journal.
        time.sleep(.2)
        self.drain()
        self.assertEqual(self.status(job)['state'],'delivered')
        self.assertEqual(self.run_command(['git','--git-dir='+str(self.remote),
                         'rev-list','--count','main']).stdout.strip(),'2')

    def paused_admission_after_disable(self, operation):
        # Pause expensive real runtime snapshotting before the acceptance commit.
        import sqlite3
        self.enable(); owner,_=self.create(); self.cli('submit',owner)
        script='''import sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from harness_integration_queue import Queue,QueueError
q=Queue(Path(sys.argv[2])); original=q.pin
def pause(engine):
    Path(sys.argv[5]).touch()
    while not Path(sys.argv[6]).exists():time.sleep(.02)
    return original(engine)
q.pin=pause
try:
    if sys.argv[7]=='integrate':q.enqueue(Path(sys.argv[3]),sys.argv[4],Path(sys.argv[8]))
    else:q.control(Path(sys.argv[3]),sys.argv[4],Path(sys.argv[8]),'gc',{'retention':0})
except (QueueError,FileNotFoundError):sys.exit(2)
'''
        child=subprocess.Popen([sys.executable,'-B','-c',script,str(ROOT/'bin'),str(self.queue),
              str(self.state),owner,str(self.base/'paused'),str(self.base/'resume'),operation,str(ENGINE)],
              env=self.env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,start_new_session=True)
        self.children.append(child)
        self.until(lambda:(self.base/'paused').exists())
        self.cli('queue','disable','--home',self.queue)
        (self.base/'resume').touch()
        stdout,stderr=child.communicate(timeout=45)
        self.assertEqual(child.returncode,2,stderr+stdout)
        with sqlite3.connect(self.queue/'queue.sqlite3') as db:
            self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0],0)

    def test_disable_fences_inflight_integration_admission(self):
        self.paused_admission_after_disable('integrate')

    def test_disable_fences_inflight_control_admission(self):
        self.paused_admission_after_disable('gc')

    def test_missing_publish_proof_keeps_uncertainty_without_resubmission(self):
        import sqlite3
        self.enable(); owner,_=self.create(); job=self.enqueue(owner)
        hook=self.remote/'hooks/post-receive'
        hook.write_text('#!/bin/bash\ntouch "'+str(self.base/'pushed')+'"\nwhile [[ ! -f "'+str(self.base/'push-release')+'" ]]; do sleep .05; done\n')
        hook.chmod(0o755)
        worker=self.start_worker();self.until(lambda:(self.base/'pushed').exists())
        worker.kill();worker.wait(timeout=10)
        os.killpg(self.status(job)['worker_pid'],signal.SIGKILL)
        record=self.state/'sessions'/owner
        (record/'pending-sha').unlink();(record/'.candidate-manifest').unlink()
        # Simulate the loss of non-durable proof after the remote accepted push.
        time.sleep(.2);self.drain()
        self.assertEqual(self.status(job)['state'],'needs_reconcile')
        self.assertIn('state=INTEGRATING',(record/'journal').read_text())
        self.assertEqual(self.run_command(['git','--git-dir='+str(self.remote),'rev-list','--count','main']).stdout.strip(),'2')

    def test_crash_after_completed_manifest_before_journal_reuses_write_ahead_proof(self):
        # Inject at the durable completed-manifest boundary in a copied runtime.
        import shutil
        global ENGINE
        original=ENGINE
        copied=self.base/'runtime/bin';shutil.copytree(ROOT/'bin',copied,ignore=shutil.ignore_patterns('__pycache__'))
        ENGINE=copied/'session-isolation.sh'
        self.addCleanup(lambda:globals().__setitem__('ENGINE',original))
        text=ENGINE.read_text()
        barrier='fsync_paths "$dir/delivered-sha" "$dir/delivered-manifest" "$dir" || return 4'
        self.assertEqual(text.count(barrier),2)
        injection='\n    if [[ ! -f "$dir/fixture-stopped" ]]; then touch "$dir/fixture-stopped"; kill -9 "$BASHPID"; fi'
        ENGINE.write_text(text.replace(barrier,barrier+injection))
        self.enable();owner,_=self.create();job=self.enqueue(owner)
        self.drain()
        self.assertEqual(self.status(job)['state'],'delivered')
        self.assertEqual(self.status(job)['attempt'],2)
        self.assertEqual(self.run_command(['git','--git-dir='+str(self.remote),'rev-list','--count','main']).stdout.strip(),'2')

    def test_recover_and_discard_use_coordinator_while_legacy_gate_is_held(self):
        # Break caught: admin commands wait on their own service's permanent old lock.
        self.enable(); owner,_=self.create({'reject':'yes'}); job=self.enqueue(owner)
        worker=self.start_worker()
        self.until(lambda:self.status(job)['state']=='failed')
        self.cli('recover',owner,timeout=15)
        self.assertIn('state=OPEN',(self.state/'sessions'/owner/'journal').read_text())
        self.cli('exit',owner)
        self.cli('discard',owner,timeout=15)
        self.assertIn('state=DISCARDED',(self.state/'sessions'/owner/'journal').read_text())
        worker.terminate();worker.wait(timeout=10)

    def test_gc_cannot_retire_accepted_work_and_finishes_through_coordinator(self):
        # Break caught: GC races queued data or deadlocks behind the coordinator gate.
        self.enable(); owner,root=self.create(); job=self.enqueue(owner)
        worker=self.start_worker()
        self.cli('gc',env=dict(self.env,HARNESS_SESSION_RETENTION_SECONDS='0'),timeout=30)
        self.assertTrue((self.queue/'jobs'/job/'input/submission.patch').exists())
        self.assertEqual(self.status(job)['state'],'delivered')
        self.assertTrue((self.state/'sessions'/owner/'journal').exists())
        worker.terminate();worker.wait(timeout=10)

    def test_busy_legacy_pool_prevents_same_remote_in_another_pool_from_starting(self):
        # Break caught: an unavailable old pool gate is skipped before destination FIFO.
        import fcntl
        self.enable(); other_env=self.enable(state=self.base/'profile-state')
        first,_=self.create({'first':'legacy'}); first_job=self.enqueue(first)
        other,_=self.create({'second':'profile'},env=other_env); second_job=self.enqueue(other,env=other_env)
        with (self.state/'integration.lock').open('a') as held:
            fcntl.flock(held,fcntl.LOCK_EX)
            self.drain()
            self.assertEqual(self.status(first_job)['state'],'queued')
            self.assertEqual(self.status(second_job)['state'],'queued')
        self.drain()
        self.assertEqual(self.status(second_job)['state'],'delivered')

    def test_open_but_unowned_descriptors_and_wrong_runtime_or_pool_cannot_enter(self):
        # Break caught: same-inode descriptors are mistaken for inherited owned leases.
        import sqlite3
        self.enable(); owner,_=self.create({'block':'yes'}); job=self.enqueue(owner)
        worker=self.start_worker();self.until(lambda:(self.base/'verifier-started').exists())
        with sqlite3.connect(self.queue/'queue.sqlite3') as db:
            pin,attempt=db.execute('SELECT pin,attempt FROM jobs WHERE job_id=?',(job,)).fetchone()
        pinned=self.queue/'runtimes'/pin/'bin/session-isolation.sh'
        paths=(self.queue/'coordinator.lock',self.state/'sessions'/owner/'broker.lock',
               self.state/'integration.lock',self.state/'queue-operations.lock')
        fds=tuple(os.open(path,os.O_RDWR) for path in paths)
        env=dict(self.env,HARNESS_INTEGRATION_HOME=str(self.queue),HARNESS_INTEGRATION_JOB=job,
                 HARNESS_INTEGRATION_ATTEMPT=str(attempt),HARNESS_INTEGRATION_LEASES=','.join(map(str,fds)))
        try:
            result=self.run_command([pinned,'_queue-execute','integrate',owner],env=env,check=False,pass_fds=fds)
            self.assertEqual(result.returncode,2,result.stderr)
            self.assertIn('does not own',result.stderr)
            result=self.run_command([ENGINE,'_queue-execute','integrate',owner],env=env,check=False,pass_fds=fds)
            self.assertEqual(result.returncode,2,result.stderr)
            self.assertIn('another runtime',result.stderr)
            wrong=dict(env,HARNESS_SESSION_STATE_HOME=str(self.base/'wrong-state'))
            result=self.run_command([pinned,'_queue-execute','integrate',owner],env=wrong,check=False,pass_fds=fds)
            self.assertEqual(result.returncode,2,result.stderr)
            self.assertIn('another state pool',result.stderr)
        finally:
            for fd in fds:os.close(fd)
        (self.base/'release').touch()
        self.until(lambda:self.status(job)['state']=='delivered')
        worker.terminate();worker.wait(timeout=10)

    def test_dead_broker_does_not_release_budget_while_verifier_group_lives(self):
        # Break caught: a broker shell dies but its verifier still consumes the slot.
        self.enable(jobs=1)
        owner,_=self.create({'block':'yes','ignore-term':'yes'}); job=self.enqueue(owner)
        other_source,_=self.repository('other')
        other,_=self.create(source=other_source); later=self.enqueue(other)
        worker=self.start_worker();self.until(lambda:(self.base/'verifier-started').exists())
        broker=self.owned_broker(worker,owner)
        os.kill(broker,signal.SIGKILL)
        deadline=time.monotonic()+10
        while True:
            rows=self.run_command(['ps','-axo','pgid=,stat=']).stdout.splitlines()
            live=any(len(parts:=line.split())==2 and parts[0]==str(broker) and not parts[1].startswith('Z') for line in rows)
            if not live:break
            self.assertEqual(self.status(job)['state'],'running')
            self.assertEqual(self.status(later)['state'],'queued')
            self.assertLess(time.monotonic(),deadline,'owned verifier group was not cleaned')
            time.sleep(.05)
        (self.base/'release').touch()
        self.until(lambda:self.status(later)['state']=='delivered')
        self.assertEqual(self.status(job)['state'],'delivered')
        worker.terminate();worker.wait(timeout=10)

    def test_crash_after_remote_push_before_ack_is_reconciled_without_second_commit(self):
        # Break caught: an uncertain successful push gets resubmitted after a crash.
        self.enable(); owner,_=self.create(); job=self.enqueue(owner)
        hook=self.remote/'hooks/post-receive'
        hook.write_text('#!/bin/bash\ntouch "'+str(self.base/'pushed')+'"\nwhile [[ ! -f "'+str(self.base/'push-release')+'" ]]; do sleep .05; done\n')
        hook.chmod(0o755)
        worker=self.start_worker();self.until(lambda:(self.base/'pushed').exists())
        record=self.state/'sessions'/owner
        pending=(record/'pending-sha').read_text().strip()
        self.assertEqual(self.run_command(['git','--git-dir='+str(self.remote),'rev-parse','main']).stdout.strip(),pending)
        self.assertFalse((record/'push-ack').exists())
        os.killpg(self.owned_broker(worker,owner),signal.SIGKILL)
        self.until(lambda:self.status(job)['state']=='delivered')
        self.assertEqual(self.run_command(['git','--git-dir='+str(self.remote),'rev-list','--count','main']).stdout.strip(),'2')
        self.assertEqual((record/'delivered-sha').read_text().strip(),pending)
        worker.terminate();worker.wait(timeout=10)

    def test_trusted_ssh_host_alias_and_https_share_destination_identity(self):
        # Break caught: configured SSH aliases create two lanes for the same repository.
        import sqlite3
        self.enable(); first,_=self.create({'a':'one'}); second,_=self.create({'b':'two'})
        ssh=self.home/'.ssh';ssh.mkdir(mode=0o700)
        (ssh/'config').write_text('Host queue-alias\n  HostName github.com\n  User git\n')
        self.run_command(['git','-C',self.source,'remote','set-url','origin','git@queue-alias:demo/repo.git'])
        one=self.enqueue(first)
        self.run_command(['git','-C',self.source,'remote','set-url','origin','https://github.com/demo/repo.git'])
        two=self.enqueue(second)
        with sqlite3.connect(self.queue/'queue.sqlite3') as db:
            destinations=[row[0] for row in db.execute('SELECT destination FROM jobs WHERE job_id IN (?,?)',(one,two))]
        self.assertEqual(len(destinations),2)
        self.assertEqual(destinations[0],destinations[1])

    def test_remote_advance_rebuilds_reverifies_and_exposes_running_phase(self):
        # Break caught: publishing a candidate verified against an obsolete remote base.
        self.enable(); owner,_=self.create({'block':'yes'}); job=self.enqueue(owner)
        worker=self.start_worker();self.until(lambda:(self.base/'verifier-started').exists())
        self.assertEqual(self.status(job).get('phase'),'verifying')
        (self.source/'external').write_text('remote actor')
        self.run_command(['git','-C',self.source,'add','external'])
        self.run_command(['git','-C',self.source,'commit','-qm','external advance'])
        self.run_command(['git','-C',self.source,'push','-q','origin','main'])
        (self.base/'release').touch()
        self.until(lambda:self.status(job)['state']=='delivered')
        self.assertEqual((self.base/'verifications').read_text().splitlines(),['verified','verified'])
        self.assertEqual(self.run_command(['git','--git-dir='+str(self.remote),'show','main:external']).stdout,'remote actor')
        worker.terminate();worker.wait(timeout=10)

    def test_disable_refuses_pending_work_then_restores_legacy_without_losing_history(self):
        # Break caught: rollback drops accepted work or leaves upgraded clients stuck.
        self.enable(); owner,_=self.create(); job=self.enqueue(owner)
        refused=self.cli('queue','disable','--home',self.queue,check=False)
        self.assertEqual(refused.returncode,2,refused.stderr)
        self.assertTrue((self.state/'integration-queue.json').exists())
        self.drain()
        self.cli('queue','disable','--home',self.queue)
        self.assertFalse((self.state/'integration-queue.json').exists())
        self.assertEqual(self.status(job)['state'],'delivered')
        other,_=self.create({'after-rollback':'legacy'})
        self.cli('close',other)
        self.assertEqual(self.run_command(['git','--git-dir='+str(self.remote),'show','main:after-rollback']).stdout,'legacy')

    def test_verifier_receives_neither_internal_authority_environment_nor_gate_fds(self):
        # Break caught: candidate tests can unlock/reuse inherited queue authority.
        self.enable(); owner,_=self.create({'check-no-authority':'yes'}); job=self.enqueue(owner)
        self.drain()
        self.assertEqual(self.status(job)['state'],'delivered')


if __name__=='__main__': unittest.main()
