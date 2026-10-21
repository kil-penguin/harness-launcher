"""Bounded, report-before-apply sweeps invoked by a verified managed entry."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parent))
from harness_session_report import build_report
from harness_session_archive import verify as verify_archive
from harness_session_pools import check_path

MAINTENANCE_API=1


def private_directory(path):
    check_path(path)
    path.mkdir(parents=True,mode=0o700,exist_ok=True)
    info=path.stat()
    if info.st_uid!=os.getuid() or info.st_mode&0o077:raise ValueError('maintenance directory is not private')


def publish(path,value):
    private_directory(path.parent)
    temporary=path.with_name('.'+str(uuid.uuid4())+'.tmp')
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    try:
        with os.fdopen(fd,'w') as stream:
            json.dump(value,stream,sort_keys=True);stream.write('\n');stream.flush();os.fsync(stream.fileno())
        # Run names are unique; last-run is an atomic advisory pointer only.
        if path.is_symlink():raise ValueError('redirected maintenance receipt')
        os.replace(temporary,path)
        directory=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:temporary.unlink(missing_ok=True)


def sweep(engine,scopes,diagnostics,*,apply=False,max_owners=10,max_seconds=300,
          retention=86400,runtime_home=None,pin=None):
    """Default is observation only. Admission must verify the selected engine.

    This module never chooses a PATH/old-bound engine, registers a job, or
    deletes snapshots/archives. GC alone owns per-root revalidation and deletion.
    """
    if not 1<=max_owners<=10 or not 1<=max_seconds<=300 or not 0<=retention<=604800:
        raise ValueError('maintenance budget')
    start=time.monotonic();deadline=start+max_seconds
    diagnostics=Path(diagnostics);private_directory(diagnostics)
    fd=os.open(diagnostics/'sweep.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_nlink!=1 or stat.S_IMODE(info.st_mode)!=0o600:
            raise ValueError('unsafe sweep lock')
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return {'mode':'busy','retired':[],'elapsed_seconds':round(time.monotonic()-start,6)}
        return sweep_locked(Path(engine),scopes,diagnostics,apply,max_owners,deadline,retention,runtime_home,pin,start)
    finally:os.close(fd)


def sweep_locked(engine,scopes,diagnostics,apply,maximum,deadline,retention,runtime_home,pin,start):
    run=str(uuid.uuid4());reports=[];candidates=[];seen=set()
    sources={}
    for scope in scopes:
        source=Path(scope['source_root']);state=Path(scope['state_root'])
        if not source.is_absolute() or not state.is_absolute():raise ValueError('absolute maintenance scope required')
        sources[(str(state),str(source))]=scope['profile']
    for state_name in dict.fromkeys(str(scope['state_root']) for scope in scopes):
        state=Path(state_name);check_path(state)
        if not state.exists():continue
        remaining=deadline-time.monotonic()
        if remaining<=0:break
        report=build_report(state=state,runtime_home=runtime_home,retention=retention,
                            max_nodes=100000,max_seconds=min(60,remaining))
        reports.append(report)
        for row in report['sessions']:
            profile=sources.get((state_name,row['source_root']))
            key=(state_name,row['session_id'])
            if profile is None or key in seen or row['eligibility']!='needs_provenance_recovery':continue
            seen.add(key)
            candidates.append({'profile':profile,'state_root':state_name,'source_root':row['source_root'],
                               'session_id':row['session_id'],'root':row['root']})
    before=diagnostics/(run+'.before.json')
    publish(before,{'schema':1,'run_id':run,'mode':'apply' if apply else 'report-only','runtime':pin,
                    'reports':reports,'candidates':candidates,'observed_elapsed_seconds':round(time.monotonic()-start,6)})
    result={'schema':1,'run_id':run,'mode':'apply' if apply else 'report-only','before_report':str(before),
            'candidate_count':len(candidates),'attempted':[],'retired':[],'elapsed_seconds':None}
    if apply:
        for row in candidates:
            if len(result['attempted'])>=maximum or time.monotonic()>=deadline:break
            started=time.monotonic()
            env={'HOME':str(Path.home()),'PATH':os.environ.get('PATH','/usr/bin:/bin'),
                 'HARNESS_SESSION_STATE_HOME':row['state_root'],'HARNESS_SESSION_RETENTION_SECONDS':str(retention),
                 'HARNESS_HISTORY_SOURCE_ROOT':row['source_root'],'PYTHONDONTWRITEBYTECODE':'1'}
            # The engine's own Native verifier has bounded RPC. Do not kill an
            # in-flight archive/rename/delete when the new-work deadline passes.
            done=subprocess.run([str(engine),'gc','--owner',row['session_id'],'--source-root',row['source_root']],
                                env=env,capture_output=True,text=True)
            attempt=dict(row,returncode=done.returncode,elapsed_seconds=round(time.monotonic()-started,6),status='retained')
            summary=re.search(r'gc: scanned=([0-9]+) retired=([0-9]+) retained=([0-9]+)',done.stderr)
            if done.returncode==0 and summary and summary[2]=='1':
                state=Path(row['state_root']);archive=state/'archives'/row['session_id']
                manifest=verify_archive(archive=archive,owner_id=row['session_id'],source_root=Path(row['source_root']))
                if os.path.lexists(row['root']):raise ValueError('retirement root remains')
                attempt['status']='retired'
                result['retired'].append(dict(row,archive=str(archive/'manifest.json'),
                    archive_files=len(manifest['files']),work_proof=str(state/'sessions'/row['session_id']/'work-proof.json')))
            result['attempted'].append(attempt)
            publish(diagnostics/(run+'.progress.json'),result)
    result['elapsed_seconds']=round(time.monotonic()-start,6)
    result['deadline_reached']=time.monotonic()>=deadline
    publish(diagnostics/(run+'.result.json'),result)
    publish(diagnostics/'last-run.json',result)
    return result
