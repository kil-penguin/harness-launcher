#!/usr/bin/env python3
"""Fail-closed archival of native Codex transcript state before session GC."""
import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

MAX_FILE_BYTES = 1024 * 1024 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
MAX_FILES = 10_000
MAX_DEPTH = 12
MAX_NODES = 20_000
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
CHUNK = 1024 * 1024
MANIFEST_VERSION = 1
CODEX_TREES = ('codex/sessions', 'codex/archived_sessions')
CODEX_FILES = ('codex/history.jsonl', 'codex/session_index.jsonl')
UUID = re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$')


class ArchiveError(RuntimeError):
    pass


def _same_owner(info):
    return info.st_uid == os.getuid()


def _regular(path):
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not _same_owner(info) or info.st_mode & 0o022:
        raise ArchiveError(f'unsafe archive source: {path}')
    return info


def _directory(path):
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or not _same_owner(info) or info.st_mode & 0o022:
        raise ArchiveError(f'unsafe archive directory: {path}')


def _identity(owner_id, source_root):
    if not isinstance(owner_id, str) or not UUID.fullmatch(owner_id):
        raise ArchiveError('invalid archive owner identity')
    source = Path(source_root)
    _directory(source)
    if source.is_symlink():
        raise ArchiveError('invalid archive source identity')


def _stamp(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _open_regular(path):
    before = _regular(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    info = os.fstat(fd)
    if _stamp(info) != _stamp(before) or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o022:
        os.close(fd)
        raise ArchiveError('archive file changed before reading')
    return fd, before


def _hash_file(path, limit=None):
    """Bounded descriptor hashing shared by verification and current-source proof."""
    limit = MAX_FILE_BYTES if limit is None else limit
    fd, before = _open_regular(path)
    digest, copied = hashlib.sha256(), 0
    with os.fdopen(fd, 'rb', buffering=0) as stream:
        if before.st_size > limit: raise ArchiveError('archive byte limit exceeded')
        while chunk := stream.read(CHUNK):
            copied += len(chunk)
            if copied > limit: raise ArchiveError('archive byte limit exceeded')
            digest.update(chunk)
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise ArchiveError('archive file changed during reading')
    if copied != before.st_size or _stamp(_regular(path)) != _stamp(before):
        raise ArchiveError('archive file changed during reading')
    return copied, digest.hexdigest()


def _json_owned(path, limit):
    def no_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result: raise ArchiveError('duplicate archive metadata key')
            result[key] = value
        return result
    fd, before = _open_regular(path)
    with os.fdopen(fd, 'rb') as stream:
        if before.st_size > limit: raise ArchiveError('archive metadata size limit exceeded')
        raw = stream.read(limit + 1)
        if len(raw) > limit or _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise ArchiveError('archive metadata changed while reading')
    if _stamp(_regular(path)) != _stamp(before):
        raise ArchiveError('archive metadata changed while reading')
    try:
        return json.loads(raw, object_pairs_hook=no_duplicates)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ArchiveError('invalid archive metadata') from exc


def _allowed_relative(relative):
    if not isinstance(relative, str) or not relative or '\\' in relative or any(ord(char) < 32 or ord(char) == 127 for char in relative): return False
    parts = relative.split('/')
    if any(part in ('', '.', '..') for part in parts) or len(parts) > MAX_DEPTH: return False
    return (len(parts) >= 3 and '/'.join(parts[:2]) in CODEX_TREES
            or relative in CODEX_FILES
            or len(parts) == 2 and bool(re.fullmatch(r'codex/state[A-Za-z0-9_.-]*\.sqlite(?:-(?:wal|shm))?', relative)))


def _walk(base, depth_offset=0, budget=None, private=False):
    """Walk without following links or allocating an unbounded directory listing."""
    stack = [(Path(base), depth_offset)]
    budget = [0] if budget is None else budget
    while stack:
        current, depth = stack.pop(); _directory(current)
        if private and stat.S_IMODE(current.lstat().st_mode) != 0o700:
            raise ArchiveError('archive directory is not private')
        with os.scandir(current) as entries:
            for entry in entries:
                budget[0] += 1
                if budget[0] > MAX_NODES: raise ArchiveError('archive node limit exceeded')
                path = Path(entry.path)
                if depth + 1 > MAX_DEPTH: raise ArchiveError('archive depth limit exceeded')
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    _directory(path); stack.append((path, depth + 1))
                elif stat.S_ISREG(info.st_mode):
                    if private and stat.S_IMODE(info.st_mode) != 0o600:
                        raise ArchiveError('archive file is not private')
                    _regular(path); yield path
                else: raise ArchiveError('unsafe archive inventory node')


def _private_parents(directory):
    missing = []; current = Path(directory)
    while not current.exists(): missing.append(current); current = current.parent
    _directory(current)
    for path in reversed(missing): path.mkdir(mode=0o700)
    _directory(directory)


def _candidate_files(root):
    _directory(root)
    base = root / '.harness'
    if not os.path.lexists(base):
        return []
    _directory(base)
    codex = base / 'codex'
    if not os.path.lexists(codex): return []
    _directory(codex)
    files, budget = [], [0]
    for relative in CODEX_TREES:
        tree = base / relative
        if not os.path.lexists(tree):
            continue
        _directory(tree)
        for path in _walk(tree, len(tree.relative_to(base).parts), budget):
            relative_depth = len(path.relative_to(base).parts)
            if relative_depth > MAX_DEPTH:
                raise ArchiveError(f'archive depth exceeds {MAX_DEPTH}: {path}')
            files.append(path)
            if len(files) > MAX_FILES: raise ArchiveError('archive file count exceeded')
    for relative in CODEX_FILES:
        path = base / relative
        if os.path.lexists(path):
            _regular(path)
            files.append(path)
    with os.scandir(codex) as entries:
        for entry in entries:
            budget[0] += 1
            if budget[0] > MAX_NODES: raise ArchiveError('archive node limit exceeded')
            if entry.name.startswith('state') and '.sqlite' in entry.name:
                path = Path(entry.path); _regular(path); files.append(path)
                if len(files) > MAX_FILES: raise ArchiveError('archive file count exceeded')
    if len(files) > MAX_FILES:
        raise ArchiveError(f'archive file count exceeds {MAX_FILES}')
    if any(not _allowed_relative(str(path.relative_to(base))) for path in files):
        raise ArchiveError('archive source is outside the native allowlist')
    return sorted(files)


def _copy_file(source, destination, total):
    fd, info = _open_regular(source)
    if info.st_size > MAX_FILE_BYTES or total + info.st_size > MAX_TOTAL_BYTES:
        os.close(fd)
        raise ArchiveError('archive byte limit exceeded')
    digest = hashlib.sha256()
    copied = 0
    with os.fdopen(fd, 'rb', buffering=0) as incoming:
        _private_parents(destination.parent)
        with open(destination, 'xb', buffering=0) as outgoing:
            while chunk := incoming.read(CHUNK):
                if copied + len(chunk) > MAX_FILE_BYTES or total + copied + len(chunk) > MAX_TOTAL_BYTES:
                    raise ArchiveError('archive source grew beyond byte limits')
                outgoing.write(chunk)
                digest.update(chunk)
                copied += len(chunk)
            outgoing.flush()
            os.fsync(outgoing.fileno())
            if _stamp(os.fstat(incoming.fileno())) != _stamp(info):
                raise ArchiveError('archive source changed while copying')
    os.chmod(destination, 0o600)
    after = _regular(source)
    if _stamp(after) != _stamp(info) or copied != info.st_size:
        raise ArchiveError(f'archive source changed while copying: {source}')
    return copied, digest.hexdigest()


def _write_manifest(directory, manifest):
    path = directory / 'manifest.json'
    payload = json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode() + b'\n'
    if len(payload) > MAX_MANIFEST_BYTES: raise ArchiveError('archive manifest size limit exceeded')
    with open(path, 'xb', buffering=0) as out:
        out.write(payload)
        out.flush()
        os.fsync(out.fileno())
    os.chmod(path, 0o600)


def _valid_imports(value, owner_id):
    if not isinstance(value, list) or len(value) > MAX_FILES: raise ArchiveError('invalid restored import proof')
    seen = set()
    for item in value:
        if (not isinstance(item, dict) or set(item) != {'runtime', 'native_id', 'source_owner'} or item['runtime'] != 'codex'
                or not isinstance(item['native_id'], str) or not UUID.fullmatch(item['native_id'])
                or not isinstance(item['source_owner'], str) or not UUID.fullmatch(item['source_owner'])
                or item['source_owner'].lower() == owner_id.lower() or item['native_id'].lower() in seen):
            raise ArchiveError('invalid restored import proof')
        seen.add(item['native_id'].lower())
    return value


def _restored_imports(state, owner_id, source_root):
    path = Path(state) / 'sessions' / owner_id / 'restored-native-sessions'
    if not os.path.lexists(path): return []
    try:
        _directory(path.parent); _directory(path.parent.parent)
        value = _json_owned(path, 65536)
        if (not isinstance(value, dict) or set(value) != {'schema','session_id','source_root','imports'}
                or type(value['schema']) is not int or value['schema'] != 1 or value['session_id'] != owner_id
                or value['source_root'] != str(Path(source_root).resolve())):
            raise ArchiveError('invalid restored import binding')
        return _valid_imports(value['imports'], owner_id)
    except (OSError, TypeError, ValueError) as exc:
        raise ArchiveError('invalid restored import ledger') from exc


def _private_json(path, limit):
    before = _regular(path)
    if stat.S_IMODE(before.st_mode) != 0o600:
        raise ArchiveError('native restore metadata is not private')
    value = _json_owned(path, limit)
    size, digest = _hash_file(path, limit)
    after = _regular(path)
    if (_stamp(before), before.st_mode, before.st_uid, before.st_nlink) != (
            _stamp(after), after.st_mode, after.st_uid, after.st_nlink):
        raise ArchiveError('native restore metadata changed while reading')
    return value, digest


def _selection(value):
    if (not isinstance(value, dict) or set(value) != {'native_id', 'snapshot_id'}
            or not isinstance(value['native_id'], str) or not UUID.fullmatch(value['native_id'])
            or not isinstance(value['snapshot_id'], str) or not re.fullmatch('[0-9a-f]{64}', value['snapshot_id'])):
        raise ArchiveError('invalid native restore selection')
    return value


def _native_restore_proof(state, owner_id, source_root):
    """Read only bounded metadata; never consult copied context to assign ownership."""
    _identity(owner_id, source_root)
    state = Path(state)
    try:
        _directory(state)
        for directory in (state / 'sessions', state / 'sessions' / owner_id):
            try:
                _directory(directory)
                with os.scandir(directory) as entries:
                    next(entries, None)
            except FileNotFoundError:
                return None
        receipt = state / 'sessions' / owner_id / 'native-history-restore.json'
        try:
            os.lstat(receipt)
        except FileNotFoundError:
            return None
        value, receipt_hash = _private_json(receipt, 65536)
        required = {'native_id', 'snapshot_id', 'destination_sha256'}
        if (not isinstance(value, dict) or not required <= set(value) or set(value) - required - {'origin_runtime'}
                or not isinstance(value['destination_sha256'], str)
                or not re.fullmatch('[0-9a-f]{64}', value['destination_sha256'])
                or ('origin_runtime' in value and (not isinstance(value['origin_runtime'], str)
                    or not re.fullmatch('[0-9a-f]{40}|legacy-unknown', value['origin_runtime'])))):
            raise ArchiveError('invalid native restore receipt')
        selected = _selection({key: value[key] for key in ('native_id', 'snapshot_id')})
        store = state / 'native-history'
        snapshot = store / 'snapshots' / selected['snapshot_id']
        for directory in (store, store / 'snapshots', snapshot):
            _directory(directory)
            if stat.S_IMODE(directory.lstat().st_mode) != 0o700:
                raise ArchiveError('native snapshot metadata is not private')
        manifest, manifest_hash = _private_json(snapshot / 'manifest.json', MAX_MANIFEST_BYTES)
        if (not isinstance(manifest, dict) or type(manifest.get('schema_version')) is not int
                or manifest['schema_version'] != 1 or manifest.get('snapshot_id') != selected['snapshot_id']
                or manifest.get('source_root') != str(Path(source_root).resolve())):
            raise ArchiveError('native restore snapshot binding mismatch')
        encoded = json.dumps({key: item for key, item in manifest.items() if key != 'snapshot_id'},
                             sort_keys=True, separators=(',', ':')).encode()
        if hashlib.sha256(encoded).hexdigest() != selected['snapshot_id']:
            raise ArchiveError('native restore snapshot hash mismatch')
        return selected, receipt_hash, manifest_hash
    except (OSError, TypeError, ValueError, KeyError) as exc:
        raise ArchiveError('invalid native restore metadata') from exc


def native_restore_selection(state, owner_id, source_root):
    proof = _native_restore_proof(state, owner_id, source_root)
    return proof[0] if proof else None


def _native_id(path):
    if path.suffix != '.jsonl':
        return None
    try:
        fd, before = _open_regular(path)
        with os.fdopen(fd, 'rb') as stream:
            prefix = stream.read(64 * 1024)
            if _stamp(os.fstat(stream.fileno())) != _stamp(before) or _stamp(_regular(path)) != _stamp(before): return None
            for line in prefix.splitlines():
                try: event = json.loads(line)
                except ValueError: continue
                payload = event.get('payload') if isinstance(event, dict) else None
                value = payload.get('id') if isinstance(payload, dict) else None
                if event.get('type') == 'session_meta' and isinstance(value, str) and UUID.fullmatch(value):
                    return value
    except (ArchiveError, OSError, ValueError, TypeError):
        return None
    return None


def _grant(path, owner_id, source_root, state):
    native_id = _native_id(path)
    helper = Path(__file__).with_name('harness-launch-record')
    if native_id is None or not helper.is_file() or helper.is_symlink():
        return None
    env = {'HOME': os.environ.get('HOME', ''), 'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
           'HARNESS_SESSION_STATE_HOME': str(state)}
    try:
        done = subprocess.run([sys.executable, str(helper), 'read', 'codex', native_id], env=env,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=3, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    fields = dict(line.split('=', 1) for line in done.stdout.splitlines() if '=' in line)
    if fields.get('source_root') != str(source_root.resolve()) or fields.get('harness_session_id', '').lower() != owner_id.lower():
        return None
    allowed = ('permission', 'approval', 'sandbox', 'network', 'bypass', 'profile', 'context')
    return {'native_id': native_id, **{key: fields[key] for key in allowed if key in fields}}


def _fsync_tree(directory):
    for path in sorted((p for p in directory.rglob('*') if p.is_dir()), reverse=True):
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _valid_grants(grants):
    if not isinstance(grants, dict) or set(grants) != {'codex'} or not isinstance(grants['codex'], list) or len(grants['codex']) > MAX_FILES:
        raise ArchiveError('invalid archive grant proof')
    allowed = {'native_id','permission','approval','sandbox','network','bypass','profile','context'}
    choices = {'permission':('default','acceptEdits','plan','auto','dontAsk','bypassPermissions'),
               'approval':('untrusted','on-failure','on-request','never'),
               'sandbox':('read-only','workspace-write','danger-full-access'),
               'network':('disabled','enabled'),
               'bypass':('1',),'profile':('fast','base','sol','astra','plan','rich'),'context':('auto','272k','500k','1m')}
    seen = set()
    for grant in grants['codex']:
        if (not isinstance(grant, dict) or set(grant)-allowed or not isinstance(grant.get('native_id'),str)
                or not UUID.fullmatch(grant['native_id']) or grant['native_id'].lower() in seen
                or any(not isinstance(value,str) or value not in choices[key] for key,value in grant.items() if key != 'native_id')):
            raise ArchiveError('invalid archive native grant')
        seen.add(grant['native_id'].lower())


def verify(*, archive, owner_id, source_root):
    archive = Path(archive)
    try:
        _identity(owner_id, source_root); _directory(archive)
        manifest = _json_owned(archive / 'manifest.json', MAX_MANIFEST_BYTES)
        required = {'version','session_id','source_root','grant_provenance','grant_provenance_sha256','files'}
        if (not isinstance(manifest,dict) or not required <= set(manifest) or set(manifest)-required-{'restored_native_imports','native_restore_selection'}
                or type(manifest.get('version')) is not int or manifest['version'] != MANIFEST_VERSION
                or manifest['session_id'] != owner_id or manifest['source_root'] != str(Path(source_root).resolve())):
            raise ArchiveError('archive manifest identity mismatch')
        grants = manifest['grant_provenance']; _valid_grants(grants)
        encoded_grants = json.dumps(grants,sort_keys=True,separators=(',',':')).encode()
        if manifest['grant_provenance_sha256'] != hashlib.sha256(encoded_grants).hexdigest():
            raise ArchiveError('archive grant provenance mismatch')
        _valid_imports(manifest.get('restored_native_imports',[]), owner_id)
        if 'native_restore_selection' in manifest: _selection(manifest['native_restore_selection'])
        files = manifest['files']
        if not isinstance(files,list) or len(files)>MAX_FILES: raise ArchiveError('invalid archive file inventory')
        actual, total = set(), 0
        for item in files:
            if (not isinstance(item,dict) or set(item)!={'path','bytes','sha256'} or not _allowed_relative(item.get('path'))
                    or type(item['bytes']) is not int or item['bytes']<0 or item['bytes']>MAX_FILE_BYTES
                    or not isinstance(item['sha256'],str) or not re.fullmatch('[0-9a-f]{64}',item['sha256']) or item['path'] in actual):
                raise ArchiveError('invalid archive file entry')
            total += item['bytes']
            if total>MAX_TOTAL_BYTES: raise ArchiveError('archive total byte limit exceeded')
            relative = Path(item['path']); parent = archive
            for part in relative.parts[:-1]:
                parent = parent / part; _directory(parent)
            copied,digest = _hash_file(archive/relative)
            if copied!=item['bytes'] or digest!=item['sha256']: raise ArchiveError('archive content proof mismatch')
            actual.add(item['path'])
        found = set()
        for path in _walk(archive,private=True):
            if path == archive/'manifest.json': continue
            relative = str(path.relative_to(archive))
            if not _allowed_relative(relative): raise ArchiveError('archive inventory is outside native allowlist')
            found.add(relative)
            if len(found)>MAX_FILES: raise ArchiveError('archive file count exceeded')
        if actual!=found: raise ArchiveError('archive inventory mismatch')
        return manifest
    except (OSError,ValueError,TypeError,KeyError,UnicodeError) as exc:
        raise ArchiveError('invalid archive proof') from exc


def _assert_current_source(root,files,records,state,owner_id,source_root,imports,selection_proof):
    if _native_restore_proof(state,owner_id,source_root) != selection_proof:
        raise ArchiveError('native restore ownership changed during archive')
    if files!=_candidate_files(root):raise ArchiveError('archive source inventory changed')
    current=[];total=0
    for source in files:
        size,digest=_hash_file(source);total+=size
        if total>MAX_TOTAL_BYTES:raise ArchiveError('archive byte limit exceeded')
        current.append({'path':str(source.relative_to(root/'.harness')),'bytes':size,'sha256':digest})
    if current!=sorted(records,key=lambda item:item['path']) or files!=_candidate_files(root):
        raise ArchiveError('archive does not preserve current live history')
    if _restored_imports(state,owner_id,source_root)!=imports:
        raise ArchiveError('archive import proof no longer matches source')


def archive(*, root, state, owner_id, source_root):
    root, state, source_root = Path(root), Path(state), Path(source_root)
    _identity(owner_id,source_root)
    _directory(root)
    base = root / '.harness'
    state.mkdir(parents=True,exist_ok=True,mode=0o700)
    _directory(state)
    selection_proof = _native_restore_proof(state,owner_id,source_root)
    selected = selection_proof[0] if selection_proof else None
    files = _candidate_files(root)
    archives = state / 'archives'
    archives.mkdir(parents=True, exist_ok=True, mode=0o700)
    _directory(archives)
    os.chmod(archives, 0o700)
    target = archives / owner_id
    if target.exists():
        manifest=verify(archive=target,owner_id=owner_id,source_root=source_root)
        if manifest.get('native_restore_selection') != selected:
            raise ArchiveError('archive native restore ownership mismatch')
        _assert_current_source(root,files,manifest['files'],state,owner_id,source_root,manifest.get('restored_native_imports',[]),selection_proof)
        return target
    with tempfile.TemporaryDirectory(prefix=f'.{owner_id}.', dir=archives) as temp:
        staging = Path(temp)
        os.chmod(staging, 0o700)
        records, total = [], 0
        for source in files:
            relative = source.relative_to(base)
            copied, digest = _copy_file(source, staging / relative, total)
            total += copied
            records.append({'path': str(relative), 'bytes': copied, 'sha256': digest})
        grants = [grant for source in files if (grant := _grant(source, owner_id, source_root, state))]
        provenance = {'codex': grants}
        manifest = {'version': MANIFEST_VERSION, 'source_root': str(source_root.resolve()),
                    'session_id': owner_id, 'files': records, 'grant_provenance': provenance,
                    'restored_native_imports': _restored_imports(state, owner_id, source_root),
                    'grant_provenance_sha256': hashlib.sha256(json.dumps(
                        provenance, sort_keys=True, separators=(',', ':')).encode()).hexdigest()}
        if selected is not None: manifest['native_restore_selection'] = selected
        _write_manifest(staging, manifest)
        verify(archive=staging, owner_id=owner_id, source_root=source_root)
        _assert_current_source(root,files,records,state,owner_id,source_root,manifest['restored_native_imports'],selection_proof)
        _fsync_tree(staging)
        os.replace(staging, target)
        _fsync_tree(archives)
    verify(archive=target, owner_id=owner_id, source_root=source_root)
    return target


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('archive', 'verify'))
    parser.add_argument('--root', type=Path)
    parser.add_argument('--state', type=Path)
    parser.add_argument('--owner', required=True)
    parser.add_argument('--source', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'archive':
            if args.root is None or args.state is None:
                parser.error('archive requires --root and --state')
            archive(root=args.root, state=args.state, owner_id=args.owner, source_root=args.source)
        else:
            if args.state is None:
                parser.error('verify requires --state')
            verify(archive=args.state / 'archives' / args.owner, owner_id=args.owner, source_root=args.source)
    except ArchiveError as exc:
        print(f'harness-session archive: {exc}', file=os.sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
