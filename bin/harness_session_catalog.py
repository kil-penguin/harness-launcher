#!/usr/bin/env python3
"""Read-only, bounded inventory of launcher-owned native sessions."""
import hashlib
import fcntl
import json
import os
import re
import stat
import sys
from itertools import islice
from typing import Dict, Optional
from pathlib import Path

if __package__:
    from .harness_session_archive import ArchiveError, verify, native_restore_selection
else:
    from harness_session_archive import ArchiveError, verify, native_restore_selection

MAX_SESSIONS = 1000
PREFIX_BYTES = 64 * 1024
MAX_NODES = 10_000
UUID = re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')


def imported_ids(record_dir, source_root, owner_id,strict=False):
    data = _safe_prefix(Path(record_dir) / 'restored-native-sessions')
    if data is None and not os.path.lexists(Path(record_dir)/'restored-native-sessions'):return set()
    try:
        value = json.loads(data) if data else None
        if not isinstance(value, dict) or value.get('schema') != 1 or value.get('session_id') != owner_id:
            raise ValueError()
        if value.get('source_root') != str(Path(source_root).resolve()) or not isinstance(value.get('imports'), list):
            raise ValueError()
        result=set()
        for item in value['imports']:
            if not isinstance(item,dict) or item.get('runtime')!='codex' or not isinstance(item.get('native_id'),str) or not UUID.fullmatch(item['native_id']) or not isinstance(item.get('source_owner'),str) or not UUID.fullmatch(item['source_owner']):raise ValueError()
            result.add(item['native_id'].lower())
        return result
    except (TypeError, ValueError):
        if strict:raise ValueError('unsafe_session_import') from None
        return set()


def _opaque(profile, runtime, owner, home, native_id, path):
    data = '/'.join((profile, runtime, owner or '', str(home), native_id, str(path)))
    return hashlib.sha256(data.encode()).hexdigest()


def _safe_prefix(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o022:
            return None
        return os.read(fd, PREFIX_BYTES)
    except OSError:
        return None
    finally:
        os.close(fd)


def _metadata(path, limit=PREFIX_BYTES):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_nlink!=1 or info.st_mode&0o022 or info.st_size>limit:
            raise ValueError('unsafe_metadata')
        return stream.read(limit+1).decode()


def _problem(problems,profile,code):
    value={'code':code,'profile':profile}
    if value not in problems:problems.append(value)


def _entries(directory,problems,profile,limit=MAX_NODES):
    try:
        with os.scandir(directory) as entries:values=list(islice(entries,limit+1))
    except OSError:
        _problem(problems,profile,'transcript_scan_failed');return []
    if len(values)>limit:_problem(problems,profile,'transcript_scan_budget')
    return sorted(values[:limit],key=lambda entry:entry.name)


def _walk(directory,problems,profile,anchor=None):
    if not os.path.lexists(directory):return
    try:
        current=directory;anchor=anchor or directory.parent
        unsafe=False
        while True:
            if current.is_symlink():unsafe=True
            if current==anchor:break
            if current==current.parent:unsafe=True;break
            current=current.parent
        if unsafe or not directory.is_dir():
            _problem(problems,profile,'unsafe_transcript_tree');return
    except OSError:
        _problem(problems,profile,'transcript_scan_failed');return
    stack,nodes=[(directory,0)],0
    while stack and nodes<MAX_NODES:
        current,depth=stack.pop()
        for entry in _entries(current,problems,profile,MAX_NODES-nodes):
            nodes+=1;path=Path(entry.path)
            try:
                if entry.is_symlink():_problem(problems,profile,'unsafe_transcript_node')
                elif entry.is_dir(follow_symlinks=False):
                    if depth<12:stack.append((path,depth+1))
                    else:_problem(problems,profile,'transcript_depth_budget')
                elif entry.name.endswith('.jsonl'):
                    if entry.is_file(follow_symlinks=False):yield path
                    else:_problem(problems,profile,'unsafe_transcript_node')
            except OSError:_problem(problems,profile,'transcript_scan_failed')
    if stack:_problem(problems,profile,'transcript_scan_budget')


def _meta(path):
    data = _safe_prefix(path)
    if data is None:
        return None
    lines = data.splitlines()
    for line in lines:
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(event, dict):
            continue
        payload = event.get('payload') if isinstance(event, dict) else None
        native_id = payload.get('id') if isinstance(payload, dict) else None
        if event.get('type') == 'session_meta' and isinstance(native_id, str) and UUID.fullmatch(native_id):
            return native_id
    return None


def _transcripts(base, problems=None, profile=None):
    problems=[] if problems is None else problems
    for name in ('sessions', 'archived_sessions'):
        yield from _walk(base/'codex'/name,problems,profile,base)


def _record(profile, owner, source, home, path, availability, archived=False, launcher_id=None):
    native_id = _meta(path)
    if native_id is None or native_id not in path.name:
        return None
    return {
        'id': _opaque(profile, 'codex', owner, home, native_id, path), 'profile': profile,
        'runtime': 'codex', 'native_id': native_id, 'launcher_session_id': launcher_id,
        'source_root': str(source), 'native_home': str(home), 'transcript_path': str(path),
        'archived': archived, 'availability': availability,
        'can_resume': availability in ('ready', 'legacy'), 'can_fork': availability in ('ready', 'archived', 'legacy'),
    }


def _lease_state(directory):
    lock = directory / 'runtime.lock'
    try:
        fd=os.open(lock,os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,'r+') as stream:
            info=os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_nlink!=1 or info.st_mode&0o022:return 'unavailable'
            fcntl.lockf(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.lockf(stream, fcntl.LOCK_UN)
        return 'free'
    except BlockingIOError:
        return 'active'
    except OSError:
        return 'unavailable'


def _source_sessions(state, profile, source, problems):
    sessions = state / 'sessions'
    worktrees = state / 'worktrees'
    if not sessions.is_dir() or sessions.is_symlink():
        return []
    records = []
    for entry in _entries(sessions,problems,profile):
        directory=Path(entry.path)
        if not directory.is_dir() or directory.is_symlink() or not UUID.fullmatch(directory.name):
            continue
        try:
            recorded=_metadata(directory/'source-root').strip()
            if recorded!=str(source):continue
            root=Path(_metadata(directory/'session-root').strip())
            state_name=next(line[6:] for line in _metadata(directory/'journal').splitlines() if line.startswith('state='))
            if state_name not in ('OPEN','ABANDONED','CONFLICT','CLOSED','SUBMITTED','INTEGRATING','DELIVERED','DISCARDED'):raise ValueError()
        except (OSError, ValueError,UnicodeError,StopIteration):
            _problem(problems,profile,'unsafe_session_record')
            continue
        if recorded != str(source) or root != worktrees / directory.name or not root.is_dir() or root.is_symlink():
            continue
        lease = _lease_state(directory)
        availability = 'unavailable' if lease == 'unavailable' or state_name in ('SUBMITTED','INTEGRATING') else ('active' if lease == 'active' or state_name == 'OPEN' or list(directory.glob('resume-hold-*.json')) else ('archived' if state_name in ('DELIVERED', 'DISCARDED') else 'ready'))
        try:imported=imported_ids(directory,source,directory.name,strict=True)
        except ValueError:
            _problem(problems,profile,'unsafe_session_import');continue
        try:selected=native_restore_selection(state,directory.name,source)
        except ArchiveError:
            _problem(problems,profile,'unsafe_native_restore');continue
        for path in _transcripts(root / '.harness', problems, profile):
            item = _record(profile, directory.name, source, root / '.harness' / 'codex', path, availability,
                           launcher_id=directory.name)
            if item and item['native_id'] in imported:
                continue
            if item and selected and item['native_id'].lower() != selected['native_id'].lower():
                continue
            if item:
                if len(records)>=MAX_SESSIONS:
                    _problem(problems,profile,'session_count_budget');return records
                if state_name in ('ABANDONED','CONFLICT'):item['can_fork']=False
                records.append(item)
            else:
                problems.append({'code': 'invalid_transcript', 'profile': profile})
    return records


def _archives(state, profile, source, problems):
    root = state / 'archives'
    if not root.is_dir() or root.is_symlink():
        return []
    records = []
    for entry in _entries(root,problems,profile):
        archive=Path(entry.path)
        if not archive.is_dir() or archive.is_symlink() or not UUID.fullmatch(archive.name):
            continue
        try:
            header=json.loads(_metadata(archive/'manifest.json',8*1024*1024))
            if isinstance(header,dict) and isinstance(header.get('source_root'),str) and header['source_root']!=str(source):continue
            manifest = verify(archive=archive, owner_id=archive.name, source_root=source)
        except (ArchiveError,OSError,ValueError,UnicodeError):
            _problem(problems,profile,'archive_verification_failed')
            continue
        listed = {entry['path'] for entry in manifest['files']}
        imported = {item['native_id'] for item in manifest.get('restored_native_imports', [])}
        selected = manifest.get('native_restore_selection')
        for path in _transcripts(archive, problems, profile):
            if str(path.relative_to(archive)) not in listed:
                _problem(problems,profile,'archive_inventory')
                continue
            item = _record(profile, archive.name, source, archive / 'codex', path, 'archived', True, archive.name)
            if item and item['native_id'] in imported:
                continue
            if item and selected and item['native_id'].lower() != selected['native_id'].lower():
                continue
            if item:
                if len(records)>=MAX_SESSIONS:
                    _problem(problems,profile,'session_count_budget');return records
                records.append(item)
    return records


def _legacy(source, profile, problems):
    records = []
    for path in _transcripts(source / '.harness', problems, profile):
        item = _record(profile, None, source, source / '.harness' / 'codex', path, 'legacy')
        if item:
            if len(records)>=MAX_SESSIONS:
                _problem(problems,profile,'session_count_budget');return records
            records.append(item)
        else:_problem(problems,profile,'invalid_transcript')
    return records


def _claude_meta(path):
    data = _safe_prefix(path)
    if not data: return None
    def find(value):
        if isinstance(value, dict):
            native_id, cwd = value.get('sessionId'), value.get('cwd')
            if isinstance(native_id, str) and UUID.fullmatch(native_id) and isinstance(cwd, str): return native_id, cwd
            for child in value.values():
                found = find(child)
                if found: return found
        elif isinstance(value, list):
            for child in value:
                found = find(child)
                if found: return found
        return None
    for line in data.splitlines():
        try: found = find(json.loads(line))
        except (ValueError,RecursionError): continue
        if found: return found
    return None


def _recorded_run_dir(path, source, root):
    """Accept recorded physical directories within this source or owner root.

    Retired roots may be absent. Resolving existing ancestors still excludes
    symlink escapes, while exact canonical spelling preserves their durable
    run-directory proof after GC removes the disposable clone.
    """
    try:
        candidate = Path(path)
        boundaries = (Path(source), Path(root))
        if not candidate.is_absolute() or str(candidate.resolve(strict=False)) != path:
            return False
        if any(not boundary.is_absolute() or boundary.resolve(strict=False) != boundary for boundary in boundaries):
            return False
        return any(candidate.is_relative_to(boundary) for boundary in boundaries)
    except (OSError, RuntimeError, ValueError):
        return False


def _provider_owners(state, source, problems, profile):
    owners = {}
    sessions = state / 'sessions'
    if not sessions.is_dir() or sessions.is_symlink():
        return owners
    for entry in _entries(sessions,problems,profile):
        directory=Path(entry.path)
        try:
            if not UUID.fullmatch(directory.name) or directory.is_symlink():
                continue
            if _metadata(directory/'source-root').strip()!=str(source):
                continue
            status=next(line[6:] for line in _metadata(directory/'journal').splitlines() if line.startswith('state='))
            if status!='OPEN':
                lease=_lease_state(directory)
                if lease=='active' or list(directory.glob('resume-hold-*.json')):status='OPEN'
                elif lease=='unavailable' or status not in ('CLOSED','ABANDONED','CONFLICT','DELIVERED','DISCARDED'):status='UNAVAILABLE'
            root = str(state / 'worktrees' / directory.name)
            allowed = [root]
            runs = directory / 'run-dirs'
            if runs.is_file() and not runs.is_symlink():
                for path in _metadata(runs).splitlines():
                    if _recorded_run_dir(path, source, root):
                        allowed.append(path)
            if not os.path.lexists(directory/'provider-sessions'):continue
            for value in _metadata(directory/'provider-sessions').splitlines():
                kind, separator, native_id = value.partition(' ')
                if kind == 'claude' and separator and UUID.fullmatch(native_id):
                    owners.setdefault(native_id.lower(), []).append((directory.name, status, allowed))
        except (OSError,ValueError,UnicodeError,StopIteration):
            _problem(problems,profile,'unsafe_session_record')
            continue
    return owners


def _claude_records(profile, source, home, problems, owners):
    base = home / '.claude' / 'projects'
    records = []
    for path in _walk(base,problems,profile):
        meta = _claude_meta(path)
        if meta is None:
            _problem(problems,profile,'invalid_claude_transcript')
            continue
        native_id, cwd = meta
        bindings = owners.get(native_id.lower(), [])
        try:
            same_source = Path(cwd).resolve(strict=True) == source
        except OSError:
            same_source = cwd == str(source)
        if len(bindings) == 1 and cwd in bindings[0][2]:
            same_source = True
        if not same_source or path.stem != native_id:
            continue
        owner, status = (bindings[0][:2] if len(bindings) == 1 else (None, None))
        availability = 'legacy' if owner is None else ('active' if status == 'OPEN' else 'unavailable' if status=='UNAVAILABLE' else ('archived' if status in ('DELIVERED', 'DISCARDED') else 'ready'))
        if len(records)>=MAX_SESSIONS:
            _problem(problems,profile,'session_count_budget');return records
        records.append({
            'id': _opaque(profile, 'claude', owner, home / '.claude', native_id, path), 'profile': profile,
            'runtime': 'claude', 'native_id': native_id, 'launcher_session_id': owner,
            'source_root': str(source), 'native_home': str(home / '.claude'), 'transcript_path': str(path),
            'archived': False, 'availability': 'unavailable' if len(bindings) > 1 else availability,
            'can_resume': len(bindings) <= 1 and availability in ('legacy', 'ready'), 'can_fork': len(bindings) <= 1 and availability in ('legacy','ready','archived'),
        })
    return records


def catalog(state: Path, sources: Dict[str, Path], home: Path) -> dict:
    state = Path(state)
    items, problems = [], []
    for profile, raw_source in sorted(sources.items()):
        source = Path(raw_source)
        try:
            source = source.resolve(strict=True)
        except OSError:
            problems.append({'kind': 'source_missing', 'profile': profile})
            continue
        items.extend(_source_sessions(state, profile, source, problems))
        items.extend(_archives(state, profile, source, problems))
        items.extend(_legacy(source, profile, problems))
        items.extend(_claude_records(profile, source, Path(home), problems, _provider_owners(state, source,problems,profile)))
    if len(items)>MAX_SESSIONS:_problem(problems,None,'session_count_budget')
    items = sorted(items, key=lambda item: item['id'])[:MAX_SESSIONS]
    revision = hashlib.sha256(json.dumps(items, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {'status': 'partial' if problems else 'ready', 'revision': revision, 'items': items, 'problems': problems}


def resolve(state: Path, sources: Dict[str, Path], home: Path, opaque_id: str) -> Optional[dict]:
    for item in catalog(state, sources, home)['items']:
        if item['id'] == opaque_id:
            return item
    return None
