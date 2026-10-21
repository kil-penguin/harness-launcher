#!/usr/bin/env python3
"""Read-only work-content proof, used under the GC owner's runtime lease."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness_session_report import metadata, root_identity_matches

UUID=re.compile(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}')
OID=re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})')
EXCLUDED=('projects','config/.local','.mcp.local.json','mcp.local.json','.claude/settings.local.json',
          '.git','.harness/codex')


class WorkError(ValueError):
    pass


def excluded(path):
    folded=path.casefold()
    return any(folded==p or folded.startswith(p+'/') for p in EXCLUDED)


def scalar(path):
    value=metadata(path).decode().removesuffix('\n')
    if not value or any(ord(c)<32 for c in value):raise WorkError('invalid metadata scalar')
    return value


def git(source,*args,index=None,objects=None,input_data=None):
    env={k:v for k,v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1',GIT_CONFIG_GLOBAL='/dev/null',GIT_TERMINAL_PROMPT='0')
    if index is not None:env['GIT_INDEX_FILE']=str(index)
    if objects is not None:
        original=Path(os.fsdecode(git(source,'rev-parse','--git-path','objects')).strip())
        original=original if original.is_absolute() else Path(source)/original
        original=original.resolve(strict=True)
        if any(c in str(original) for c in (':','\n','\r','"')):raise WorkError('unsupported object store path')
        env.update(GIT_OBJECT_DIRECTORY=str(objects),GIT_ALTERNATE_OBJECT_DIRECTORIES=str(original))
    result=subprocess.run(['/usr/bin/git','--no-optional-locks','-c','core.fsmonitor=false',
        '-c','core.hooksPath=/dev/null','-c','diff.external=','-C',str(source),*args],
        env=env,capture_output=True,input=input_data,timeout=15)
    if result.returncode or len(result.stdout)>32*1024*1024:raise WorkError('trusted git read refused')
    return result.stdout


def index_entries(source,index,objects=None):
    before=stamp(index.lstat());indexed={}
    for entry in git(source,'ls-files','--stage','-z',index=index,objects=objects).split(b'\0'):
        if not entry:continue
        fields,raw=entry.split(b'\t',1);mode,oid,stage=fields.split();path=os.fsdecode(raw)
        if stage!=b'0':raise WorkError('unmerged session index')
        if not excluded(path):indexed[path]=(mode.decode(),oid.decode())
    if stamp(index.lstat())!=before:raise WorkError('index changed')
    return indexed


def discarded_tree(source,base,patch):
    # Reconstruct only in private temporary index/objects. Canonical objects,
    # session index, worktree, configuration, filters and hooks stay untouched.
    with tempfile.TemporaryDirectory(prefix='harness-discard-proof-') as temporary:
        temporary=Path(temporary).resolve();index=temporary/'index';objects=temporary/'objects';objects.mkdir()
        git(source,'read-tree',base,index=index,objects=objects)
        git(source,'apply','--cached','--binary','--allow-empty','--whitespace=nowarn','-',
            index=index,objects=objects,input_data=patch)
        return index_entries(source,index,objects)


def tree(source,revision):
    result={}
    for entry in git(source,'ls-tree','-rz','--full-tree',revision).split(b'\0'):
        if not entry:continue
        fields,raw=entry.split(b'\t',1);mode,kind,oid=fields.split();path=os.fsdecode(raw)
        if not excluded(path):result[path]=(mode.decode(),oid.decode())
    return result


def stamp(info):
    return info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns


def object_hash(path,mode,algorithm):
    before=path.lstat()
    if mode=='120000':
        if not stat.S_ISLNK(before.st_mode):raise WorkError('symlink mode changed')
        raw=os.fsencode(os.readlink(path));result=hashlib.new(algorithm,b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()
    else:
        if mode not in ('100644','100755') or not stat.S_ISREG(before.st_mode):raise WorkError('unsupported work entry')
        if bool(before.st_mode&0o111)!=(mode=='100755'):raise WorkError('executable mode changed')
        if before.st_size>1024*1024*1024:raise WorkError('work file limit')
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,'rb') as incoming:
            if stamp(os.fstat(incoming.fileno()))!=stamp(before):raise WorkError('work entry changed')
            digest=hashlib.new(algorithm,b'blob '+str(before.st_size).encode()+b'\0');size=0
            while block:=incoming.read(1024*1024):
                size+=len(block)
                if size>before.st_size:raise WorkError('work entry grew')
                digest.update(block)
            if size!=before.st_size or stamp(os.fstat(incoming.fileno()))!=stamp(before):raise WorkError('work entry changed')
        result=digest.hexdigest()
    if stamp(path.lstat())!=stamp(before):raise WorkError('work path changed')
    return result


def prove(state,owner,retained_root=None):
    state=Path(state)
    if not UUID.fullmatch(owner):raise WorkError('invalid owner')
    record=state/'sessions'/owner;expected_root=state/'worktrees'/owner
    recorded=Path(scalar(record/'session-root'))
    if (not recorded.is_absolute() or recorded.name!=owner
            or recorded.parent.resolve(strict=True)!=expected_root.parent.resolve(strict=True)):
        raise WorkError('absolute root mismatch')
    root=Path(retained_root) if retained_root else expected_root
    root=root.parent.resolve(strict=True)/root.name
    if root!=expected_root and (root.parent!=expected_root.parent or not re.fullmatch(r'\.(?:archiving|retired)-'+re.escape(owner)+r'-[0-9]+',root.name)):
        raise WorkError('unexpected retained root')
    info=root.lstat()
    if not stat.S_ISDIR(info.st_mode) or not root_identity_matches(scalar(record/'root-inode'),info,root):raise WorkError('root identity mismatch')
    source=Path(scalar(record/'source-root'));base=scalar(record/'base-sha')
    if not OID.fullmatch(base):raise WorkError('invalid base')
    if not (root/'.git').is_dir() or (root/'.git').is_symlink():raise WorkError('session git identity changed')
    journal=dict(line.split('=',1) for line in metadata(record/'journal').decode().splitlines())
    status=journal.get('state');expected=tree(source,base);base_tree=dict(expected)
    if status!='DISCARDED' and scalar(root/'.git/HEAD')!=base:raise WorkError('session has unaccounted commits')
    if status=='DELIVERED':
        delivered=scalar(record/'delivered-sha')
        if not OID.fullmatch(delivered):raise WorkError('invalid delivered revision')
        delivered_tree=tree(source,delivered)
        fields=metadata(record/'delivered-manifest').split(b'\0')
        if fields[-1]!=b'' or (len(fields)-1)%4 or len(fields)<5:raise WorkError('invalid delivered manifest')
        for offset in range(0,len(fields)-1,4):
            kind,mode,oid,raw=fields[offset:offset+4];path=os.fsdecode(raw)
            if path.startswith('/') or any(p in ('','.','..') for p in path.split('/')) or excluded(path):raise WorkError('unsafe delivered path')
            if kind==b'D' and mode==b'-' and oid==b'-' and path not in delivered_tree:
                expected.pop(path,None)
            elif kind==b'F' and delivered_tree.get(path)==(mode.decode(),oid.decode()):
                expected[path]=(mode.decode(),oid.decode())
            else:raise WorkError('delivery content is unverified')
    elif status=='DISCARDED':
        expected=discarded_tree(source,base,metadata(record/'discarded.patch'))
    elif status!='CLOSED':raise WorkError('nonterminal work is retained')
    indexed=index_entries(source,root/'.git/index')
    if status=='DISCARDED':
        if any(indexed.get(path) not in (base_tree.get(path),expected.get(path))
               for path in set(indexed)|set(base_tree)|set(expected)):
            raise WorkError('unaccounted staged work')
    else:
        if os.path.lexists(record/'headless'):
            if any(indexed.get(path) not in (base_tree.get(path),expected.get(path))
                   for path in set(indexed)|set(base_tree)|set(expected)):
                raise WorkError('unaccounted agent index')
            indexed=index_entries(source,record/'trusted.git/index')
        if indexed!=expected:raise WorkError('unaccounted staged work')
    observed=set();nodes=0;algorithm='sha1' if len(base)==40 else 'sha256'
    def walk_error(error):raise WorkError('work inventory is inaccessible')
    for directory,dirs,files in os.walk(root,followlinks=False,onerror=walk_error):
        directory=Path(directory)
        if directory.stat().st_dev!=info.st_dev:raise WorkError('work inventory crosses a mount')
        nodes+=len(dirs)
        if nodes>100000:raise WorkError('work inventory limit')
        for name in list(dirs):
            path=directory/name;relative=str(path.relative_to(root))
            if excluded(relative):dirs.remove(name)
            elif path.is_symlink():dirs.remove(name);files.append(name)
        for name in files:
            path=directory/name;relative=str(path.relative_to(root))
            if excluded(relative):continue
            nodes+=1
            if nodes>100000 or relative not in expected:raise WorkError('unaccounted work file')
            mode,oid=expected[relative]
            if object_hash(path,mode,algorithm)!=oid:raise WorkError('unaccounted work content')
            observed.add(relative)
    if observed!=set(expected) or root.lstat().st_ino!=info.st_ino:raise WorkError('work tree changed')
    return {'status':'proven','session_id':owner,'lifecycle':status,'files':len(observed)}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--state',type=Path,required=True)
    parser.add_argument('--owner',required=True);parser.add_argument('--retained-root',type=Path)
    args=parser.parse_args()
    try:result=prove(args.state,args.owner,args.retained_root)
    except (OSError,ValueError,subprocess.SubprocessError,UnicodeError):
        print(json.dumps({'status':'retained','reason':'work_content_unverified','session_id':args.owner}));return 2
    print(json.dumps(result));return 0


if __name__=='__main__':raise SystemExit(main())
