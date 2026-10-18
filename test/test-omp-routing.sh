#!/usr/bin/env zsh
# Real dispatcher/entry, fake native only; no real OMP/auth/history is opened.
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
python3 -B - "$ROOT" <<'PYTEST'
from pathlib import Path
import hashlib, json, os, shlex, signal, subprocess, sys, tempfile, time

repo=Path(sys.argv[1]); passed=0; observations=[]
with tempfile.TemporaryDirectory(prefix='omp-routing-') as tmp:
    root=Path(tmp).resolve(); home=root/'caller-home'; profile=root/'project with spaces'; ns=root/'normal'; tools=root/'tools'; capture=root/'capture.json'; wire=root/'wire.argv'; ready=root/'ready'; release=root/'release'
    leaves=['home','agent','history','cwd','tmp','cache','config','state','data','native-cache']
    for p in [home,profile/'config',profile/'projects/sub dir',tools,ns,*[ns/x for x in leaves]]:
        p.mkdir(parents=True,exist_ok=True);p.chmod(0o700)
    binary=tools/'native-omp'
    calls=root/'native-calls.jsonl'; behavior=root/'native-behavior'
    # Use this real interpreter rather than Apple's /usr/bin/python3 shim,
    # which injects Xcode/SDK environment before it reaches the fake body.
    binary.write_text('#!'+sys.executable+'\nimport json,os,sys,signal\nfrom pathlib import Path\nPath('+repr(str(capture))+').write_text(json.dumps({"argv":sys.argv[1:],"cwd":os.getcwd(),"env":dict(os.environ)}))\nwith Path('+repr(str(calls))+').open("a") as f: f.write(json.dumps(sys.argv[1:])+"\\n")\nmode=Path('+repr(str(behavior))+').read_text() if Path('+repr(str(behavior))+').exists() else ""\nif mode=="signal": os.kill(os.getpid(),signal.SIGTERM)\nif sys.argv[1:]==["--","fixture-wait"] or mode=="wait":\n import time\n Path('+repr(str(ready))+').touch()\n while not Path('+repr(str(release))+').exists(): time.sleep(0.02)\nraise SystemExit(23 if mode=="exit" or sys.argv[1:]==["--", "fixture-exit"] else 0)\n')
    binary.chmod(0o555); digest=hashlib.sha256(binary.read_bytes()).hexdigest()
    marker=ns/'.owner.json'
    owner={'schema':'p0-normal-omp-owner-v1','identity':{'CODEX_THREAD_ID':'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'},'prefix':str(ns),'binary':str(binary),'asset_sha256':digest}
    marker.write_text(json.dumps(owner));marker.chmod(0o600)
    config=profile/'config/launcher.env'
    def configuration(**override):
        values={'HARNESS_NAME':'fixture','HARNESS_PREFIX':'fixture','HARNESS_SESSION_ISOLATION_DEFAULT':'0','HARNESS_OMP_BIN':str(binary),'HARNESS_OMP_ROOT':str(ns),'HARNESS_OMP_SHA256':digest}
        values.update(override)
        config.write_text('\n'.join(k+'='+shlex.quote(v) for k,v in values.items())+'\n')
    configuration()
    for name in ['claude','codex','gum']:
        p=tools/name;p.write_text('#!/bin/sh\nexit 97\n');p.chmod(0o700)
    env={'HOME':str(home),'PATH':str(tools)+':/opt/homebrew/bin:/usr/bin:/bin','LC_ALL':'C','TERM':'dumb','TMPDIR':str(root),'HARNESS_CLAUDE_PLUGIN_MIRROR':'0','PYTHONDONTWRITEBYTECODE':'1','OPENAI_API_KEY':'fixture-secret-must-not-leak','ANTHROPIC_API_KEY':'fixture-secret-must-not-leak','NODE_OPTIONS':'fixture-must-not-leak','HARNESS_SESSION_HEARTBEAT_SECONDS':'0.05','HARNESS_POST_EDIT_CODEX_SYNC':'0','HARNESS_POST_COMMIT_CODEX_SYNC':'0'}
    state=root/'sessions';env['HARNESS_SESSION_STATE_HOME']=str(state)
    # The new selector is activated only after first-rollout preparation. Keep
    # this legacy-routing fixture explicitly never-enrolled in its own HOME.
    import importlib.util
    registry=home/'.config/harness-launcher/profiles';registry.mkdir(parents=True,mode=0o700)
    spec=importlib.util.spec_from_file_location('omp_projection_fixture',repo/'bin/harness_omp_projection.py')
    projection=importlib.util.module_from_spec(spec);spec.loader.exec_module(projection)
    previous_home=os.environ.get('HOME');os.environ['HOME']=str(home)
    try:
        assert Path.home()==home and binary.parent==tools
        projection.Publisher().prepare({'binding_sha256':'1'*64,'review_sha256':'2'*64,'selection_sha256':'3'*64,'operation_sha256':'4'*64,'controller':{'native':'fixture','pane':'fixture','managed':'fixture'},'scope':'OMP3_CONFIGURATION','ssot':None})
    finally:
        if previous_home is None:os.environ.pop('HOME',None)
        else:os.environ['HOME']=previous_home
    expected_env={'HOME':str(ns/'home'),'PATH':'/usr/bin:/bin','LANG':'C','LC_ALL':'C','TMPDIR':str(ns/'tmp'),'XDG_CACHE_HOME':str(ns/'cache'),'XDG_CONFIG_HOME':str(ns/'config'),'XDG_STATE_HOME':str(ns/'state'),'XDG_DATA_HOME':str(ns/'data'),'PI_CODING_AGENT_DIR':str(ns/'agent'),'PI_CODING_AGENT_SESSION_DIR':str(ns/'history'),'PI_NATIVES_DIR':str(ns/'native-cache'),'TERM':'xterm-256color'}
    # Calibrate only the fake interpreter startup, not the OMP contract. macOS
    # adds this nonsecret field even to a directly launched empty-env Python.
    baseline=subprocess.run([sys.executable,'-c','import os,json;print(json.dumps(dict(os.environ)))'],env=expected_env,text=True,capture_output=True,check=True)
    fake_env=json.loads(baseline.stdout)
    assert {k:v for k,v in fake_env.items() if k in expected_env}==expected_env
    assert set(fake_env)-set(expected_env) <= {'__CF_USER_TEXT_ENCODING'}
    spy='function /usr/bin/env() { builtin printf \'%s\\0\' "$@" > '+shlex.quote(str(wire))+'; command /usr/bin/env "$@"; }; '
    def allocation():
        return [(str(p.relative_to(root)),p.is_dir(),p.stat().st_size,p.stat().st_mtime_ns)
                for base in (state,profile/'.harness',home/'.local/state') if base.exists()
                for p in sorted([base,*base.rglob('*')])]
    def count():return len(calls.read_text().splitlines()) if calls.exists() else 0
    def run(args,expected=0,entry=False,tail=None,cwd=None,no_allocate=False):
        global passed
        capture.unlink(missing_ok=True);wire.unlink(missing_ok=True)
        before=count();before_allocation=allocation()
        if entry:command=['/bin/zsh',str(repo/'bin/harness-exec'),str(profile),*args]
        else:command=['/bin/zsh','-f','-c','source "$1/bin/aliases.zsh"; '+spy+'shift; _harness_launcher_run "$@"','test',str(repo),str(profile),*args]
        result=subprocess.run(command,cwd=profile,env=env,input=b'',capture_output=True,timeout=20)
        assert result.returncode==expected,(args,result.returncode,expected,result.stderr.decode())
        assert count()-before==(0 if tail is None else 1),('native invocation count',args)
        if no_allocate:assert allocation()==before_allocation,('management allocated or changed launcher state',args)
        if tail is None:
            assert not capture.exists(),('refusal launched native',args)
        else:
            assert capture.exists(),('native not called',args,result.stderr.decode())
            got=json.loads(capture.read_text());assert got['argv']==tail,(args,got)
            assert got['cwd']==str(cwd or ns/'cwd'),(args,got['cwd'])
            assert got['env']==fake_env,(args,got['env'])
            if not entry:
                outgoing=wire.read_bytes().split(b'\0')[:-1]
                assert [x.decode() for x in outgoing]==['-i',*[k+'='+v for k,v in expected_env.items()],str(binary),*tail],(args,outgoing)
        observations.append({'input':args,'exit':result.returncode,'entry':entry,'native':json.loads(capture.read_text()) if capture.exists() else None,'outgoing':wire.read_bytes().split(b'\0')[:-1] if wire.exists() else None})
        passed+=1
    # Break caught: OMP falls through into the Claude route rather than native.
    run(['omp','--version'],tail=['--version'])
    # Exact management tuples must bypass workspace/session preparation. These
    # expectations come from the public command contract, not its classifier.
    management=[['--version'],['--help'],['login','anthropic'],['login','openai-codex'],
                ['models','ls','anthropic','--json','--kind','chat','--no-extensions'],
                ['models','ls','openai-codex','--json','--kind','chat','--no-extensions']]
    for tail in management:run(['omp',*tail],tail=tail,no_allocate=True)
    owner_id='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
    for control in [[],['--no-isolated'],['--isolated'],['--isolated-session',owner_id]]:
        for tail in management[2:]:
            run(['--cwd',str(profile/'projects/sub dir'),*control,'omp',*tail],tail=tail,no_allocate=True)
    configuration(HARNESS_SESSION_ISOLATION_DEFAULT='1')
    for tail in management[2:]:run(['omp',*tail],tail=tail,no_allocate=True)
    configuration()
    for tail in management[2:]:run(['omp',*tail],entry=True,tail=tail,no_allocate=True)
    invalid_management=[['login'],['login','other'],['login','anthropic','extra'],['login','anthropic','--'],
        ['login','--','anthropic'],['--passthrough','login','anthropic'],
        ['models'],['models','ls'],['models','ls','other','--json','--kind','chat','--no-extensions'],
        ['models','ls','anthropic'],['models','ls','anthropic','--kind','chat','--json','--no-extensions'],
        ['models','ls','anthropic','--json','--kind','embedding','--no-extensions'],
        ['models','ls','anthropic','--json','--kind','chat'],
        [*management[4],'extra'],[*management[5],'--'],['--version','extra'],['--help','--']]
    for tail in invalid_management:run(['omp',*tail],expected=2,no_allocate=True)
    # The same pin/owner/link/mode checks protect nonpublic management.
    configuration(HARNESS_OMP_SHA256='0'*64);run(['omp',*management[2]],expected=2,no_allocate=True);configuration()
    marker.write_text(json.dumps({**owner,'asset_sha256':'0'*64}));run(['omp',*management[3]],expected=2,no_allocate=True);marker.write_text(json.dumps(owner))
    marker.chmod(0o644);run(['omp',*management[4]],expected=2,no_allocate=True);marker.chmod(0o600)
    binary.chmod(0o755);run(['omp',*management[5]],expected=2,no_allocate=True);binary.chmod(0o555)
    linked_management=tools/'linked-management';linked_management.symlink_to(binary)
    configuration(HARNESS_OMP_BIN=str(linked_management));run(['omp',*management[2]],expected=2,no_allocate=True);configuration()
    linked_namespace=root/'linked-namespace';linked_namespace.symlink_to(ns)
    configuration(HARNESS_OMP_ROOT=str(linked_namespace));run(['omp',*management[2]],expected=2,no_allocate=True);configuration()
    # Exit/signal statuses propagate and only acquired kernel locks unwind.
    lock=ns/'.launcher.lock';lock_identity=(lock.stat().st_dev,lock.stat().st_ino)
    for mode,status in [('exit',23),('signal',143)]:
        behavior.write_text(mode)
        run(['omp',*management[2]],expected=status,tail=management[2],no_allocate=True)
        behavior.unlink()
        run(['omp',*management[3]],tail=management[3],no_allocate=True)
        assert (lock.stat().st_dev,lock.stat().st_ino)==lock_identity
    for args,tail in [(['omp','--help'],['--help']),(['omp'],[]),(['omp','omp','--','text'],['omp','--','text']),(['omp','--model','claude','--','a b',''],['--model','claude','--','a b','']),(['omp','--','--resume','literal'],['--','--resume','literal'])]:
        run(args,tail=tail)
    run(['omp','--','fixture-exit'],expected=23,tail=['--','fixture-exit'])
    owner_id='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
    for control in [[],['--no-isolated'],['--isolated-session',owner_id]]:
        run(['--cwd',str(profile/'projects/sub dir'),*control,'omp','--version'],tail=['--version'],cwd=profile/'projects/sub dir')
    run(['omp','--help'],entry=True,tail=['--help'],cwd=profile)
    for args in [['claude','omp'],['omp','claude'],['omp','codex'],['omp','--resume','not-a-uuid'],['omp','--model','claude','--restore','not-a-uuid'],['omp','--profile','foreign'],['omp','--session-dir','foreign'],['omp','--cwd','foreign'],['omp','--from-claude'],['omp','auth'],['omp','--unknown'],['--isolated-session'],['--cwd']]:
        run(args,expected=2)
    configuration(HARNESS_OMP_SHA256='0'*64);run(['omp','--version'],expected=2);configuration()
    marker.chmod(0o644);run(['omp','--version'],expected=2);marker.chmod(0o600)
    configuration(HARNESS_OMP_BIN=str(tools/'absent'));run(['omp','--version'],expected=2);configuration()
    linked=tools/'linked-omp';linked.symlink_to(binary)
    configuration(HARNESS_OMP_BIN=str(linked));run(['omp','--version'],expected=2);configuration()
    marker.write_text(json.dumps({**owner,'asset_sha256':'0'*64}));run(['omp','--version'],expected=2);marker.write_text(json.dumps(owner))
    configuration(HARNESS_OMP_ROOT=str(root/'not-prepared'));run(['omp','--version'],expected=2);configuration()
    # Real isolated-session helper, owner lease, cwd rebinding and finish path.
    # All repositories/history remain disposable and the native remains fake.
    subprocess.run(['git','init','-q','-b','main',str(profile)],env=env,check=True)
    subprocess.run(['git','-C',str(profile),'add','config/launcher.env'],env=env,check=True)
    subprocess.run(['git','-c','core.hooksPath=/dev/null','-c','user.name=fixture','-c','user.email=fixture@example.invalid','-C',str(profile),'commit','-qm','fixture'],env=env,check=True)
    run(['--cwd',str(profile/'projects/sub dir'),'--isolated','omp','omp','--','text'],tail=['omp','--','text'],cwd=profile/'projects/sub dir')
    records=list((state/'sessions').iterdir());assert len(records)==1,records
    actual_owner=records[0].name
    run(['--cwd',str(profile/'projects/sub dir'),'--isolated-session',actual_owner,'omp','omp','--','text'],tail=['omp','--','text'],cwd=profile/'projects/sub dir')
    assert 'state=CLOSED' in (records[0]/'journal').read_text()
    assert len(list((state/'sessions').iterdir()))==1
    configuration(HARNESS_SESSION_ISOLATION_DEFAULT='1')
    run(['omp','--model','resume','--','literal'],tail=['--model','resume','--','literal'])
    configuration()
    # A retained normal-home lease rejects a second launcher, then releases.
    command=['/bin/zsh','-f','-c','source "$1/bin/aliases.zsh"; shift; _harness_launcher_run "$@"','test',str(repo),str(profile),'omp','--','fixture-wait']
    active=subprocess.Popen(command,cwd=profile,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        deadline=time.monotonic()+10
        while not ready.exists() and active.poll() is None and time.monotonic()<deadline:time.sleep(.02)
        assert ready.exists(),('native barrier missing',active.poll())
        run(['omp'],expected=2)
        busy_identity=(lock.stat().st_dev,lock.stat().st_ino)
        for tail in management[2:]:run(['omp',*tail],expected=2,no_allocate=True)
        assert (lock.stat().st_dev,lock.stat().st_ino)==busy_identity
    finally:
        release.touch()
        out,err=active.communicate(timeout=10)
    assert active.returncode==0,(active.returncode,err)
    run(['omp'],tail=[])
    # Terminate only the owned fake process group, then prove reacquisition.
    ready.unlink();release.unlink();behavior.write_text('wait')
    signaled=subprocess.Popen(command[:-2]+management[2],cwd=profile,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
    try:
        deadline=time.monotonic()+10
        while not ready.exists() and signaled.poll() is None and time.monotonic()<deadline:time.sleep(.02)
        assert ready.exists(),('management signal barrier missing',signaled.poll())
        os.killpg(signaled.pid,signal.SIGTERM)
        out,err=signaled.communicate(timeout=10)
        assert signaled.returncode in (-signal.SIGTERM,143),(signaled.returncode,err)
    finally:
        if signaled.poll() is None:os.killpg(signaled.pid,signal.SIGKILL);signaled.communicate(timeout=10)
        behavior.unlink()
    run(['omp',*management[3]],tail=management[3],no_allocate=True)
    # A preexisting symlink/hardlink or public lock cannot be adopted.
    lock=ns/'.launcher.lock';lock.unlink();lock.symlink_to(marker)
    run(['omp',*management[2]],expected=2,no_allocate=True);assert lock.is_symlink();lock.unlink()
    os.link(marker,lock);run(['omp',*management[2]],expected=2,no_allocate=True);assert marker.stat().st_nlink==2;lock.unlink()
    lock.write_text('');lock.chmod(0o644);run(['omp',*management[2]],expected=2,no_allocate=True);assert lock.exists();lock.unlink()
    print('SAVED_FAKE_PROOF='+json.dumps({'schema':'omp-routing-fake-v1','startup_baseline':fake_env,'cases':[{**x,'outgoing':[b.decode() for b in x['outgoing']] if x['outgoing'] is not None else None} for x in observations]},separators=(',',':')))
    print('PASS:',passed,'OMP real-entry/dispatcher argv/env/cwd/exit and fail-closed cases; native fake only')
PYTEST
