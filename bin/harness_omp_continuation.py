"""Bounded OMP continuation metadata; neither target metadata nor H grants a lease.

Entry calls request/resolve before begin and exclusively publishes initial_record
after its ordinary begin. Bound aliases call operate only under the original
workspace lease and namespace lock. This module never launches a subprocess,
repairs a workspace, imports history, reads transcript bodies or changes grants.
Trusted same-UID kernel/config state remains the existing data TCB.
"""
import copy
import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys
import uuid

_spec = importlib.util.spec_from_file_location('_omp_projection', Path(__file__).with_name('harness_omp_projection.py'))
p = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(p)

LIMIT = 16384
PROTOCOL = 'omp-launcher-continuation/v2'
CONTEXT_KEYS = ('schema profile_binding_sha256 host_binding_sha256 namespace_path namespace_identity '
                'namespace_marker_sha256 binary_path binary_sha256 binary_size binary_release '
                'current_sha256 snapshot_revision manifest_sha256 consumer_sha256 projection_helper_sha256 '
                'continuation_helper_sha256 index_generation state_generation enrollment_generation lane '
                'valid_until provenance_sha256')
ORIGIN_KEYS = ('handle source_root state_root launcher_session_id session_dir workspace_root workspace_identity '
               'runtime_pin_revision runtime_manifest_sha256 runtime_lock_identity source_root_marker_sha256 '
               'session_root_marker_sha256 original_binding_sha256 profile_binding_sha256 host_binding_sha256 '
               'binary_sha256 namespace_identity namespace_marker_sha256')
RECORD_KEYS = ('schema status handle kernel_origin origin_context_provenance last_admitted_context_provenance '
               'native_identity lifecycle_nonce last_terminal_status last_observation_grade')
PROVENANCE_KEYS = ('profile_binding_sha256 host_binding_sha256 binary_sha256 namespace_marker_sha256 '
                   'current_sha256 snapshot_revision manifest_sha256 index_generation state_generation '
                   'enrollment_generation lane valid_until provenance_sha256')
NATIVE_KEYS = 'id path cwd version timestamp header_sha256 file_identity'
FILE_KEYS = 'dev ino uid gid nlink mode size mtime_ns ctime_ns'
STABLE = ('dev', 'ino', 'uid', 'gid', 'nlink', 'mode')
OPTIONS = frozenset(('--model --smol --slow --plan --provider --models --thinking '
                     '--service-tier --tools --skills --max-time').split())
SWITCHES = frozenset(('-p --print --no-tools --no-lsp --no-pty --no-extensions --no-skills '
                      '--no-rules --no-title --hide-thinking --no-prewalk').split())


class ContinuationError(ValueError):
    def __init__(self, label='OMP_CONTINUATION_UNBOUND'):
        self.label = label
        super().__init__(label)


def require(value, label='OMP_CONTINUATION_UNBOUND'):
    if not value:
        raise ContinuationError(label)


def keys(value, expected):
    require(type(value) is dict and set(value) == set(expected.split()))


def number(value, positive=False):
    require(type(value) is int and (1 if positive else 0) <= value <= 2**64-1)


def handle(value):
    require(type(value) is str and re.fullmatch('[0-9a-f]{32}', value), 'OMP_CONTINUATION_INVALID_HANDLE')
    return value


def nonce(value):
    handle(value)


def workspace_id(value):
    require(type(value) is str and re.fullmatch(
        '[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}', value))
    return value


def request(args):
    """Exact lexical public argv; None leaves every preexisting route alone."""
    args = list(args)
    offset = 2 if args[:1] == ['--cwd'] else 0
    if args[offset:offset+1] in (['--isolated-session'],): offset += 2
    elif args[offset:offset+1] in (['--isolated'], ['--no-isolated']): offset += 1
    if args[offset:offset+1] != ['omp']:
        return None
    tail = args[offset+1:]
    # Flags/values are consumed once below; prompt contents remain opaque.
    if not tail or tail[0] not in ('--session-new', '--session-resume'):
        for item in tail:
            if item == '--': break
            require(item not in ('--session-new', '--session-resume'))
        return None
    require(offset == 0)
    mode = 'NEW' if tail.pop(0) == '--session-new' else 'RESUME'
    h = None
    if mode == 'RESUME':
        require(bool(tail)); h = handle(tail.pop(0))
    options = []
    while tail and tail[0] != '--':
        value = tail.pop(0)
        require(type(value) is str and '\0' not in value and len(value.encode('utf-8')) <= 65536)
        if value in OPTIONS:
            require(bool(tail) and not tail[0].startswith('-'))
            literal = tail.pop(0)
            require('\0' not in literal and len(literal.encode('utf-8')) <= 65536)
            options += [value, literal]
        elif value in SWITCHES:
            options.append(value)
        else:
            flag, sep, literal = value.partition('=')
            require(sep == '=' and flag in OPTIONS and bool(literal))
            options.append(value)
        require(len(options) <= 128)
    require(bool(tail) and tail.pop(0) == '--' and len(tail) <= 1)
    for prompt in tail:
        require(type(prompt) is str and '\0' not in prompt and len(prompt.encode('utf-8')) <= 65536)
    return {'mode': mode, 'handle': h, 'options': options, 'prompt': tail}


def file_identity(meta):
    return {'dev': meta.st_dev, 'ino': meta.st_ino, 'uid': meta.st_uid,
            'gid': meta.st_gid, 'nlink': meta.st_nlink, 'mode': stat.S_IMODE(meta.st_mode),
            'size': meta.st_size, 'mtime_ns': meta.st_mtime_ns, 'ctime_ns': meta.st_ctime_ns}


def check_file_identity(value):
    keys(value, FILE_KEYS)
    for item in value.values(): number(item)
    require(value['uid'] == os.getuid() and value['nlink'] == 1 and value['mode'] == 0o600)


def owned_directory(path, private=False):
    path = p.physical(path); meta = path.lstat()
    require(stat.S_ISDIR(meta.st_mode) and meta.st_uid == os.getuid() and not meta.st_mode & 0o022)
    if private: require(stat.S_IMODE(meta.st_mode) == 0o700)
    return p.identity(meta)


def read_metadata(path):
    return p.read_file(path, None, LIMIT)[0]


def checked_context(value):
    keys(value, CONTEXT_KEYS)
    require(value['schema'] == 'omp-continuation-context/v1')
    for name in ('profile_binding_sha256', 'host_binding_sha256', 'namespace_marker_sha256',
                 'binary_sha256', 'current_sha256', 'manifest_sha256', 'consumer_sha256',
                 'projection_helper_sha256', 'continuation_helper_sha256', 'provenance_sha256'):
        p.sha(value[name])
    for name in ('index_generation', 'state_generation', 'enrollment_generation'):
        number(value[name], True); require(value[name] <= 2**63-1)
    number(value['binary_size'], True)
    require(re.fullmatch('[0-9a-f]{40}', value['snapshot_revision']))
    require(value['lane'] in ('BOOTSTRAP_ACTIVE', 'SSOT_ACTIVE')); p.fresh(value['valid_until'])
    p.physical(value['binary_path']); p.physical(value['namespace_path'])
    keys(value['namespace_identity'], 'dev ino uid mode')
    for item in value['namespace_identity'].values(): number(item)
    require(value['namespace_identity']['mode'] == 0o700)
    p.check_binary_release(value['binary_release'])
    return value


def current_context(source, consumer):
    try:
        return checked_context(p.select_continuation_source(str(source), str(p.registry_dir()), str(source), str(consumer)))
    except ContinuationError: raise
    except p.ProjectionError as exc:
        raise ContinuationError(exc.label) from None
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        raise ContinuationError() from None


def provenance(context):
    return {key: copy.deepcopy(context[key]) for key in PROVENANCE_KEYS.split()}


def check_provenance(value):
    keys(value, PROVENANCE_KEYS)
    for key in PROVENANCE_KEYS.split():
        if key.endswith('sha256'): p.sha(value[key])
    for key in ('index_generation', 'state_generation', 'enrollment_generation'):
        number(value[key], True); require(value[key] <= 2**63-1)
    require(re.fullmatch('[0-9a-f]{40}', value['snapshot_revision']))
    require(value['lane'] in ('BOOTSTRAP_ACTIVE', 'SSOT_ACTIVE')); p.expiry(value['valid_until'])


def binding(state, source, w, consumer, context):
    """Repeat physical origin checks, independently of the caller's metadata."""
    state = p.physical(state); source = p.physical(source); consumer = p.physical(consumer)
    workspace_id(w); h = w.replace('-', '').lower()
    directory = state/'sessions'/w; work = state/'worktrees'/w
    for path in (state, state/'sessions', directory, state/'worktrees', work, source): owned_directory(path)
    src = read_metadata(directory/'source-root'); dst = read_metadata(directory/'session-root')
    require(src.decode('utf-8').strip() == str(source) and dst.decode('utf-8').strip() == str(work),
            'OMP_CONTINUATION_CWD_MISMATCH')
    require(read_metadata(directory/'lease-v1') == b'1\n')
    lock = directory/'runtime.lock'; read_metadata(lock)
    lock_meta = lock.lstat()
    # Existing isolation metadata permits 0600/0644, without changing that policy.
    require(stat.S_ISREG(lock_meta.st_mode) and lock_meta.st_uid == os.getuid() and
            lock_meta.st_nlink == 1 and not lock_meta.st_mode & 0o022)
    bound_path = consumer.parent.parent.parent/'bindings'/(w.lower()+'.json')
    raw, _ = p.read_file(bound_path, limit=LIMIT); bound = p.decode(raw)
    keys(bound, 'source_root runtime'); keys(bound['runtime'], 'version revision path manifest_sha256')
    require(bound['source_root'] == str(source) and bound['runtime']['path'] == str(consumer.parent.parent))
    require(bound['runtime']['revision'] == context['snapshot_revision'] and
            bound['runtime']['manifest_sha256'] == context['manifest_sha256'])
    return {'handle': h, 'source_root': str(source), 'state_root': str(state),
            'launcher_session_id': w, 'session_dir': str(directory), 'workspace_root': str(work),
            'workspace_identity': owned_directory(work), 'runtime_pin_revision': context['snapshot_revision'],
            'runtime_manifest_sha256': context['manifest_sha256'], 'runtime_lock_identity': file_identity(lock_meta),
            'source_root_marker_sha256': p.digest(src), 'session_root_marker_sha256': p.digest(dst),
            'original_binding_sha256': p.digest(raw), 'profile_binding_sha256': context['profile_binding_sha256'],
            'host_binding_sha256': context['host_binding_sha256'], 'binary_sha256': context['binary_sha256'],
            'namespace_identity': copy.deepcopy(context['namespace_identity']),
            'namespace_marker_sha256': context['namespace_marker_sha256']}


def initial_record(state, source, w, consumer):
    """Return data for Entry's exclusive post-begin publication; never write it."""
    try:
        context = current_context(source, consumer)
        origin = binding(state, source, w, consumer, context)
        return {'schema': PROTOCOL, 'status': 'PENDING', 'handle': origin['handle'], 'kernel_origin': origin,
                'origin_context_provenance': provenance(context), 'last_admitted_context_provenance': provenance(context),
                'native_identity': None, 'lifecycle_nonce': uuid.uuid4().hex,
                'last_terminal_status': None, 'last_observation_grade': 'NOT_OBSERVED'}
    except (p.ProjectionError, OSError, ValueError, TypeError, KeyError) as exc:
        if isinstance(exc, ContinuationError): raise
        raise ContinuationError() from None


def check_record(row):
    keys(row, RECORD_KEYS); require(row['schema'] == PROTOCOL)
    require(row['status'] in ('PENDING', 'READY', 'IN_FLIGHT', 'BLOCKED'))
    handle(row['handle']); nonce(row['lifecycle_nonce'])
    keys(row['kernel_origin'], ORIGIN_KEYS); require(row['kernel_origin']['handle'] == row['handle'])
    check_provenance(row['origin_context_provenance']); check_provenance(row['last_admitted_context_provenance'])
    require(row['last_terminal_status'] is None or type(row['last_terminal_status']) is int and
            0 <= row['last_terminal_status'] <= 255)
    require(row['last_observation_grade'] in ('NOT_OBSERVED', 'SAME_FILE', 'NATIVE_NONZERO',
            'NOT_PERSISTED', 'IDENTITY_CHANGED', 'NATIVE_FORK', 'POSTCHECK_FAILED'))
    if row['native_identity'] is not None: check_native_identity(row['native_identity'])
    require(row['status'] not in ('READY', 'IN_FLIGHT') or row['native_identity'] is not None)
    require(row['status'] != 'READY' or row['last_terminal_status'] == 0)
    return row


def load_record(state, source, h, consumer):
    handle(h); state = p.physical(state); owned_directory(state)
    context = current_context(source, consumer)
    candidate = str(uuid.UUID(hex=h))
    found = {}
    for w in dict.fromkeys((candidate, candidate.upper())):
        path = state/'sessions'/w
        if os.path.lexists(path):
            identity = owned_directory(path)
            found[(identity['dev'], identity['ino'])] = path
    require(len(found) == 1, 'OMP_CONTINUATION_FOREIGN')
    directory = next(iter(found.values()))
    raw, _ = p.read_file(directory/'omp-continuation.json', limit=LIMIT)
    row = check_record(p.decode(raw)); origin = row['kernel_origin']
    workspace_id(origin['launcher_session_id'])
    require(origin['launcher_session_id'].replace('-', '').lower() == h and
            Path(origin['session_dir']) == state/'sessions'/origin['launcher_session_id'])
    actual = binding(state, source, origin['launcher_session_id'], consumer, context)
    require(origin == actual and row['handle'] == h, 'OMP_CONTINUATION_IDENTITY_CHANGED')
    # Return the authoritative spelling, not a case-normalized replacement.
    path = Path(origin['session_dir'])/'omp-continuation.json'
    require(p.read_file(path, limit=LIMIT)[0] == raw)
    return path, raw, row, context


def selection(row, raw, context):
    origin = row['kernel_origin']; h = row['handle']
    sessions = Path(context['namespace_path'])/'launcher-continuations'/h/'sessions'
    return {'schema': 'omp-continuation-selection/v1', 'handle': h, 'status': row['status'],
            'launcher_session_id': origin['launcher_session_id'], 'workspace_root': origin['workspace_root'],
            'session_dir': str(sessions), 'native_path': row['native_identity']['path'] if row['native_identity'] else None,
            'record_sha256': p.digest(raw), 'lifecycle_nonce': row['lifecycle_nonce']}


def resolve(state, source, h, consumer):
    """Untrusted candidate until Entry also verifies its own store.bound checks."""
    try:
        _, raw, row, context = load_record(state, source, h, consumer)
        require(row['status'] == 'READY', 'OMP_CONTINUATION_IN_FLIGHT_UNRESOLVED')
        same_native(row['native_identity'], inspect_native(
            Path(context['namespace_path'])/'launcher-continuations'/h/'sessions', Path(row['kernel_origin']['workspace_root'])))
        return selection(row, raw, context)
    except ContinuationError: raise
    except (p.ProjectionError, OSError, ValueError, TypeError, KeyError):
        raise ContinuationError() from None


def check_native_identity(value):
    keys(value, NATIVE_KEYS)
    require(type(value['id']) is str and str(uuid.UUID(value['id'])) == value['id'])
    require(type(value['version']) is int and value['version'] == 3)
    p.physical(value['path']); p.physical(value['cwd']); p.text(value['timestamp']); p.sha(value['header_sha256'])
    check_file_identity(value['file_identity'])


def header_line(fd):
    # Unbuffered one-byte reads stop exactly at LF: no buffered body read-ahead.
    raw = bytearray()
    while len(raw) <= 65536:
        byte = os.read(fd, 1)
        require(bool(byte), 'OMP_CONTINUATION_NOT_PERSISTED')
        raw.extend(byte)
        if byte == b'\n':
            require(len(raw) <= 65536, 'OMP_CONTINUATION_NOT_PERSISTED')
            return bytes(raw)
    raise ContinuationError('OMP_CONTINUATION_NOT_PERSISTED')


def _inspect_native(sessions, cwd):
    """At most16 entries and exactly one eligible v3 JSONL; header only."""
    sessions = p.physical(sessions); cwd = p.physical(cwd)
    for directory in (sessions.parent.parent, sessions.parent, sessions): owned_directory(directory, True)
    owned_directory(cwd)
    entries = []
    with os.scandir(sessions) as scan:
        for item in scan:
            require(len(entries) < 16, 'OMP_CONTINUATION_NOT_PERSISTED')
            entries.append(item.name)
    matches = [sessions/name for name in entries if name.endswith('.jsonl')]
    require(len(matches) == 1, 'OMP_CONTINUATION_NATIVE_FORK' if len(matches) > 1 else 'OMP_CONTINUATION_NOT_PERSISTED')
    path = p.physical(matches[0]); before = path.lstat()
    require(stat.S_ISREG(before.st_mode) and before.st_uid == os.getuid() and
            before.st_nlink == 1 and stat.S_IMODE(before.st_mode) == 0o600 and before.st_size <= 1048576,
            'OMP_CONTINUATION_NOT_PERSISTED')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        require(p.stamp(before) == p.stamp(os.fstat(fd)))
        header_line(fd)  # discard the native title, never decode or retain it
        raw = header_line(fd)
        require(p.stamp(before) == p.stamp(os.fstat(fd)) == p.stamp(path.lstat()),
                'OMP_CONTINUATION_IDENTITY_CHANGED')
    finally: os.close(fd)
    value = p.decode(raw)
    require(type(value) is dict and set(value) in (
        {'type','version','id','cwd','timestamp'}, {'type','version','id','cwd','timestamp','parentSession'}))
    require(value.get('parentSession') is None, 'OMP_CONTINUATION_NATIVE_FORK')
    require(value['type'] == 'session' and type(value['version']) is int and value['version'] == 3)
    native_id = value['id']; require(type(native_id) is str and str(uuid.UUID(native_id)) == native_id)
    require(path.name.endswith('_'+native_id+'.jsonl') and value['cwd'] == str(cwd), 'OMP_CONTINUATION_CWD_MISMATCH')
    p.text(value['timestamp']); require(len(value['timestamp'].encode('utf-8')) <= 64)
    require(re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,9})?Z', value['timestamp']))
    datetime.datetime.fromisoformat(value['timestamp'].replace('Z', '+00:00'))
    result = {'id': native_id, 'path': str(path), 'cwd': str(cwd), 'version': 3,
              'timestamp': value['timestamp'], 'header_sha256': p.digest(raw), 'file_identity': file_identity(before)}
    check_native_identity(result)
    return result


def inspect_native(sessions, cwd):
    try:
        return _inspect_native(sessions, cwd)
    except ContinuationError: raise
    except (p.ProjectionError, OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        raise ContinuationError('OMP_CONTINUATION_NOT_PERSISTED') from None


def same_native(old, new):
    require({key: value for key, value in old.items() if key != 'file_identity'} ==
            {key: value for key, value in new.items() if key != 'file_identity'} and
            all(old['file_identity'][key] == new['file_identity'][key] for key in STABLE),
            'OMP_CONTINUATION_IDENTITY_CHANGED')


def replace_record(path, old_raw, row):
    raw = p.canonical(row); require(len(raw) <= LIMIT)
    require(p.read_file(path, limit=LIMIT)[0] == old_raw, 'OMP_CONTINUATION_STALE')
    # Kernel session directories retain their existing safe permission policy.
    owned_directory(path.parent)
    temp = path.with_name('.omp-continuation-'+uuid.uuid4().hex+'.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        require(p.read_file(path, limit=LIMIT)[0] == old_raw, 'OMP_CONTINUATION_STALE')
        os.replace(temp, path); p.sync_directory(path.parent)
    finally: temp.unlink(missing_ok=True)
    return raw


def operate(value):
    """Closed private-fd ABI. Caller must hold workspace then namespace locks."""
    try:
        keys(value, 'schema mode state_root source_root launcher_session_id consumer handle expected_sha256 lifecycle_nonce terminal_status')
        require(value['schema'] == 'omp-continuation-operation/v1')
        mode = value['mode']; require(mode in ('RESOLVE', 'NEW_PREPARE', 'RESUME_ADMIT', 'FINALIZE'))
        path, raw, row, context = load_record(value['state_root'], value['source_root'], value['handle'], value['consumer'])
        require(row['kernel_origin']['launcher_session_id'] == value['launcher_session_id'])
        p.sha(value['expected_sha256']); nonce(value['lifecycle_nonce'])
        require(p.digest(raw) == value['expected_sha256'] and row['lifecycle_nonce'] == value['lifecycle_nonce'],
                'OMP_CONTINUATION_STALE')
        sessions = Path(context['namespace_path'])/'launcher-continuations'/row['handle']/'sessions'
        cwd = Path(row['kernel_origin']['workspace_root'])
        if mode != 'FINALIZE': require(value['terminal_status'] is None)
        if mode == 'NEW_PREPARE':
            require(row['status'] == 'PENDING' and row['native_identity'] is None)
            container = sessions.parent.parent
            if not os.path.lexists(container): container.mkdir(mode=0o700)
            owned_directory(container, True)
            sessions.parent.mkdir(mode=0o700)  # exclusive; no adoption/cleanup
            sessions.mkdir(mode=0o700)
        elif mode == 'RESUME_ADMIT':
            require(row['status'] == 'READY', 'OMP_CONTINUATION_IN_FLIGHT_UNRESOLVED')
            observed = inspect_native(sessions, cwd); same_native(row['native_identity'], observed)
            row.update(status='IN_FLIGHT', lifecycle_nonce=uuid.uuid4().hex, native_identity=observed,
                       last_terminal_status=None, last_admitted_context_provenance=provenance(context))
            raw = replace_record(path, raw, row)
        elif mode == 'RESOLVE':
            require(row['status'] in ('PENDING', 'IN_FLIGHT'))
            owned_directory(sessions.parent, True); owned_directory(sessions, True)
            if row['status'] == 'IN_FLIGHT': same_native(row['native_identity'], inspect_native(sessions, cwd))
        else:
            require(row['status'] in ('PENDING', 'IN_FLIGHT'))
            terminal = value['terminal_status']
            if terminal is None: return selection(row, raw, context)  # unresolved, byte-for-byte retained
            require(type(terminal) is int and 0 <= terminal <= 255)
            failure = None; observed = None
            try:
                observed = inspect_native(sessions, cwd)
                if row['native_identity'] is not None: same_native(row['native_identity'], observed)
            except (ContinuationError, p.ProjectionError, OSError, ValueError, TypeError, KeyError) as exc:
                failure = exc.label if isinstance(exc, ContinuationError) else 'OMP_CONTINUATION_NOT_PERSISTED'
            row.update(last_terminal_status=terminal, last_admitted_context_provenance=provenance(context))
            if terminal == 0 and failure is None:
                row.update(status='READY', native_identity=observed, last_observation_grade='SAME_FILE')
            else:
                grade = 'NATIVE_NONZERO' if terminal else (
                    'NATIVE_FORK' if failure == 'OMP_CONTINUATION_NATIVE_FORK' else
                    'IDENTITY_CHANGED' if failure == 'OMP_CONTINUATION_IDENTITY_CHANGED' else 'NOT_PERSISTED')
                row.update(status='BLOCKED', last_observation_grade=grade)
            raw = replace_record(path, raw, row)
            if failure is not None: raise ContinuationError(failure)
        return selection(row, raw, context)
    except ContinuationError: raise
    except (p.ProjectionError, OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        raise ContinuationError() from None


def main(argv):
    try:
        require(len(argv) == 3 and argv[1] == '--input-fd' and re.fullmatch('[0-9]{1,3}', argv[2]))
        fd = int(argv[2]); raw = bytearray()
        while len(raw) <= LIMIT:
            part = os.read(fd, LIMIT+1-len(raw))
            if not part: break
            raw.extend(part)
        require(len(raw) <= LIMIT)
        result = operate(p.decode(bytes(raw))); output = p.canonical(result)
        require(len(output) <= LIMIT); sys.stdout.buffer.write(output+b'\n')
        return 0
    except (ContinuationError, p.ProjectionError) as exc:
        print('harness-launcher: '+exc.label, file=sys.stderr); return 2
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        print('harness-launcher: OMP_CONTINUATION_UNBOUND', file=sys.stderr); return 2


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
