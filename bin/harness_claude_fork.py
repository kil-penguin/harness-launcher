#!/usr/bin/env python3
"""Verify explicit Claude fork origins without modifying parent history.

The caller must retain these descriptors through exec and call recheck immediately
before exec. POSIX record locks survive exec, but are not inherited by forked
children: a supervising caller must stay alive until its native child exits.
"""
import fcntl
import hashlib
import json
import re
import subprocess
import importlib
import sys
import types
import os
from pathlib import Path
import stat
import time

# A unique sibling package prevents an already-imported older runtime from
# supplying catalog/archive helpers to the selected, verified generation.
_package_name = __name__ + '_siblings'
_package = types.ModuleType(_package_name)
_package.__path__ = [str(Path(__file__).resolve().parent)]
sys.modules[_package_name] = _package
_catalog = importlib.import_module(_package_name + '.harness_session_catalog')
_context = importlib.import_module(_package_name + '.harness_launch_context')
UUID = _catalog.UUID
_claude_records, _provider_owners = _catalog._claude_records, _catalog._provider_owners

MAX_TRANSCRIPT = 256 * 1024 * 1024
MAX_METADATA = 64 * 1024
TERMINAL = {'CLOSED', 'DELIVERED', 'DISCARDED'}
_ACTIVE_PREPARED = set()
_PERMISSIONS = {'plan', 'dontAsk', 'default', 'acceptEdits', 'auto', 'bypassPermissions'}
_MODEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]*(\[1m\])?')
_EFFORTS = {'low', 'medium', 'high', 'xhigh', 'max'}
_PROBE = Path(__file__).resolve().parent / 'harness-restore-probe'


class ForkError(ValueError):
    pass


def _absolute(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ForkError('unsafe_fork_path')
    return path


def _open(path, flags=os.O_RDONLY, mode=0o600):
    """Walk ancestors with no-follow directory descriptors, including absent leaf."""
    path = _absolute(path)
    directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = next_fd
        return os.open(path.name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, mode, dir_fd=directory)
    finally:
        os.close(directory)


def _regular(info):
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o022:
        raise ForkError('unsafe_fork_file')


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode, info.st_uid, info.st_nlink)


def _read(path, limit=MAX_METADATA, private=False):
    fd = _open(path)
    try:
        before = os.fstat(fd); _regular(before)
        if private and before.st_mode & 0o077:
            raise ForkError('unsafe_fork_private_file')
        if before.st_size > limit:
            raise ForkError('fork_metadata_budget')
        data = bytearray()
        while len(data) <= limit:
            block = os.read(fd, min(64 * 1024, limit + 1 - len(data)))
            if not block: break
            data.extend(block)
        if len(data) > limit or _identity(before) != _identity(os.fstat(fd)):
            raise ForkError('fork_metadata_changed')
        return bytes(data)
    finally:
        os.close(fd)


def _directory(path):
    fd = _open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ForkError('unsafe_fork_directory')
    finally:
        os.close(fd)


def _lease(path, create=False):
    fd = _open(path, os.O_RDWR | (os.O_CREAT if create else 0))
    try:
        _regular(os.fstat(fd))
        # Duplicate BEFORE acquiring: closing any same-inode FD after lockf
        # would silently release all of this process's record locks.
        high = fcntl.fcntl(fd, fcntl.F_DUPFD, 10)
    finally:
        os.close(fd)
    try:
        fcntl.lockf(high, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.set_inheritable(high, True)
        return high
    except BaseException:
        os.close(high)
        raise


def _digest(path):
    fd = _open(path)
    try:
        before = os.fstat(fd); _regular(before)
        if before.st_size > MAX_TRANSCRIPT:
            raise ForkError('fork_transcript_budget')
        digest = hashlib.sha256(); size = 0; prefix = bytearray()
        deadline = time.monotonic() + 10
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block: break
            size += len(block)
            if size > MAX_TRANSCRIPT or time.monotonic() > deadline:
                raise ForkError('fork_transcript_budget')
            digest.update(block)
            prefix.extend(block[:max(0, MAX_METADATA-len(prefix))])
        if size != before.st_size or _identity(before) != _identity(os.fstat(fd)) or _identity(before) != _identity(os.lstat(path)):
            raise ForkError('fork_transcript_changed')
        return digest.hexdigest(), size, bytes(prefix), _identity(before)
    finally:
        os.close(fd)


def _transcript_meta(prefix):
    # Match the catalog's native metadata shape, without trusting its locator.
    def find(value):
        if isinstance(value, dict):
            native, cwd = value.get('sessionId'), value.get('cwd')
            if isinstance(native, str) and UUID.fullmatch(native) and isinstance(cwd, str):
                return native, cwd
            for child in value.values():
                result = find(child)
                if result: return result
        elif isinstance(value, list):
            for child in value:
                result = find(child)
                if result: return result
        return None
    for line in prefix.splitlines():
        try: found = find(json.loads(line))
        except (ValueError, RecursionError): continue
        if found: return found
    raise ForkError('invalid_fork_metadata')


class PreparedFork:
    def __init__(self, source, state, home, native_id, candidate, fds, locks):
        self.source, self.state, self.home = source, state, home
        self.native_id, self.candidate = native_id, candidate
        self.fds, self._locks = tuple(fds), tuple(locks)
        # Decided once: a recheck never re-evaluates the process context, it
        # only proves the record and transcript are still the ones used.
        self._top_level = bool(_context.launch_top_level()[0])
        self.inherited = None
        self.receipt, self._proof = self._verify()
        self.inherited = self._inherit()
        _ACTIVE_PREPARED.add(id(self))

    def _verify(self):
        if not self.fds:
            raise ForkError('fork_lease_closed')
        # NEVER reopen lock files or call catalog here. An open/close of a
        # runtime.lock in this PID drops our lock even through another FD.
        for fd, path in zip(self.fds, self._locks):
            info = os.fstat(fd); _regular(info)
            if _identity(info) != _identity(os.lstat(path)):
                raise ForkError('fork_lease_changed')
            _directory(path.parent)
        owner = self.candidate['launcher_session_id']
        directory = self.state / 'sessions' / owner
        _directory(directory)
        proof = {}
        for name in ('source-root', 'session-root', 'journal', 'provider-sessions'):
            proof[name] = _read(directory/name)
        if proof['source-root'].decode().strip() != str(self.source):
            raise ForkError('fork_source_mismatch')
        root = self.state/'worktrees'/owner
        if proof['session-root'].decode().strip() != str(root):
            raise ForkError('fork_owner_root_mismatch')
        states = [line[6:] for line in proof['journal'].decode().splitlines() if line.startswith('state=')]
        if len(states) != 1 or states[0] not in TERMINAL:
            raise ForkError('fork_parent_not_terminal')
        with os.scandir(directory) as entries:
            for count, entry in enumerate(entries):
                if count >= 10000: raise ForkError('fork_record_budget')
                if entry.name.startswith('resume-hold-') and entry.name.endswith('.json'):
                    raise ForkError('fork_parent_active')
        providers = [line.split() for line in proof['provider-sessions'].decode().splitlines()]
        if ['claude', self.native_id] not in [[part.lower() for part in row] for row in providers]:
            raise ForkError('fork_provider_mismatch')
        allowed = {str(root)} if _catalog._recorded_run_dir(str(root), self.source, root) else set()
        if os.path.lexists(directory/'run-dirs'):
            proof['run-dirs'] = _read(directory/'run-dirs')
            for path in proof['run-dirs'].decode().splitlines():
                if _catalog._recorded_run_dir(path, self.source, root):
                    allowed.add(path)
        path = _absolute(self.candidate['transcript_path'])
        if not path.is_relative_to(self.home/'.claude/projects') or path.stem.lower() != self.native_id:
            raise ForkError('fork_locator_mismatch')
        digest, size, prefix, identity = _digest(path)
        native, cwd = _transcript_meta(prefix)
        if native.lower() != self.native_id or cwd not in allowed:
            raise ForkError('fork_transcript_owner_mismatch')
        if self._top_level:
            proof['launch-record'] = self._record_bytes()
        receipt = dict(schema=1, runtime='claude', source_root=str(self.source), native_id=self.native_id,
                       source_owner=owner, source_status=states[0], native_home=str(self.home/'.claude'),
                       locator=str(path), locator_sha256=digest, bytes=size)
        return receipt, (proof, identity)

    def _record_bytes(self):
        """Raw origin launch record (no-follow, private), or None when absent/unsafe."""
        try:
            return _read(self.state / 'launch-records' / ('claude-' + self.native_id), private=True)
        except (ForkError, OSError):
            return None

    def _inherit(self):
        """Launch settings of the origin, only for a top-level launch and a bound record."""
        data = self._proof[0].get('launch-record') if self._top_level else None
        if data is None:
            return None
        fields = {}
        for line in data.decode('utf-8', 'replace').split('\n'):
            key, separator, value = line.partition('=')
            if separator and key in ('permission', 'source_root', 'isolated', 'harness_session_id', 'context'):
                fields[key] = value
        owner = self.candidate['launcher_session_id']
        try:
            bound = (fields.get('isolated') == '1' and os.path.isabs(fields.get('source_root', ''))
                     and os.path.realpath(fields['source_root']) == str(self.source)
                     and UUID.fullmatch(fields.get('harness_session_id', '')) is not None
                     and fields['harness_session_id'].lower() == owner.lower())
        except (OSError, ValueError):
            bound = False
        if not bound:
            return None
        inherited = {'permission': fields['permission'] if fields.get('permission') in _PERMISSIONS else None,
                     'model': None, 'effort': None}
        model, effort = self._probe()
        if model:
            if fields.get('context') == '1m' and not model.endswith('[1m]'):
                model += '[1m]'
            inherited['model'] = model
        inherited['effort'] = effort
        return inherited

    def _probe(self):
        """model/effort from the digest-verified locator; recheck() refuses a changed file."""
        model = effort = None
        try:
            out = subprocess.run([sys.executable, str(_PROBE), 'claude', self.receipt['locator']],
                                 capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            return None, None
        for line in out.splitlines():
            key, separator, value = line.partition('=')
            if key == 'model' and _MODEL.fullmatch(value) and value.startswith('claude-'):
                model = value
            elif key == 'effort' and value in _EFFORTS:
                effort = value
        return model, effort

    def recheck(self):
        try:
            receipt, proof = self._verify()
            if receipt != self.receipt or proof != self._proof:
                raise ForkError('fork_parent_changed')
        except BaseException as exc:
            self.close()
            if isinstance(exc, (OSError, UnicodeError)):
                raise ForkError('fork_parent_unavailable') from exc
            raise

    def close(self):
        _ACTIVE_PREPARED.discard(id(self))
        descriptors, self.fds = self.fds, ()
        for fd in descriptors:
            os.close(fd)


def prepare(source_root: Path, state_home: Path, native_id: str, home: Path | None = None) -> PreparedFork:
    """Acquire both leases and verify exactly one source-owned terminal parent."""
    if _ACTIVE_PREPARED:
        raise ForkError('fork_lease_already_held')
    fds = []
    try:
        if not isinstance(native_id, str) or not UUID.fullmatch(native_id):
            raise ForkError('invalid_claude_native_id')
        native_id = native_id.lower()
        source, state = Path(source_root).resolve(strict=True), _absolute(state_home)
        home = _absolute(home if home is not None else Path.home())
        _directory(state)
        for path in (state/'native-history', state/'native-history/leases'):
            try: path.mkdir(mode=0o700)
            except FileExistsError: pass
            _directory(path)
        native_lock = state/'native-history/leases'/('claude-'+native_id+'.lock')
        fds.append(_lease(native_lock, create=True))
        problems = []
        owners = _provider_owners(state, source, problems, 'fork')
        candidates = _claude_records('fork', source, home, problems, owners, native_id=native_id)
        if problems:
            raise ForkError('fork_catalog_incomplete')
        candidates = [item for item in candidates if item['native_id'].lower() == native_id]
        if len(candidates) != 1:
            raise ForkError('fork_origin_missing_or_ambiguous')
        candidate = candidates[0]
        owner = candidate['launcher_session_id']
        if not owner or not UUID.fullmatch(owner):
            raise ForkError('fork_owner_inactivity_unproven')
        if candidate['availability'] not in ('ready', 'archived') or not candidate['can_fork']:
            raise ForkError('fork_parent_not_inactive')
        runtime_lock = state/'sessions'/owner/'runtime.lock'
        fds.append(_lease(runtime_lock))
        return PreparedFork(source, state, home, native_id, candidate, fds, (native_lock, runtime_lock))
    except BaseException as exc:
        for fd in fds: os.close(fd)
        if isinstance(exc, (OSError, UnicodeError)):
            raise ForkError('fork_origin_unsafe_or_busy') from exc
        raise


def validate_receipt(receipt, owner, source_root):
    """Reject incomplete or rebound derived-origin records before provider writes."""
    fields = {'schema', 'runtime', 'source_root', 'native_id', 'source_owner', 'source_status',
              'native_home', 'locator', 'locator_sha256', 'bytes', 'owner'}
    if not isinstance(receipt, dict) or set(receipt) != fields:
        raise ForkError('invalid_fork_receipt')
    if type(receipt['schema']) is not int or receipt['schema'] != 1 or receipt['runtime'] != 'claude':
        raise ForkError('invalid_fork_receipt')
    if receipt['owner'] != owner or receipt['source_root'] != source_root:
        raise ForkError('fork_receipt_binding_mismatch')
    for key in ('owner', 'native_id', 'source_owner'):
        if not isinstance(receipt[key], str) or not UUID.fullmatch(receipt[key]):
            raise ForkError('invalid_fork_receipt')
    if receipt['owner'].lower() == receipt['source_owner'].lower() or receipt['source_status'] not in TERMINAL:
        raise ForkError('invalid_fork_receipt')
    for key in ('source_root', 'native_home', 'locator'):
        if not isinstance(receipt[key], str): raise ForkError('invalid_fork_receipt')
        _absolute(receipt[key])
    locator = Path(receipt['locator'])
    if not locator.is_relative_to(Path(receipt['native_home'])/'projects') or locator.name.lower() != receipt['native_id'].lower()+'.jsonl':
        raise ForkError('invalid_fork_receipt')
    digest = receipt['locator_sha256']
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
        raise ForkError('invalid_fork_receipt')
    if type(receipt['bytes']) is not int or not 0 < receipt['bytes'] <= MAX_TRANSCRIPT:
        raise ForkError('invalid_fork_receipt')


# Deliberately bounded launcher/native grammar: unknown options cannot authorize
# a fork. Values are consumed before examining controls, and -- ends controls.
_VALUE_OPTIONS = {
    '--model', '--effort', '--permission-mode', '--settings', '--setting-sources',
    '--mcp-config', '--allowedTools', '--allowed-tools', '--disallowedTools',
    '--disallowed-tools', '--tools', '--system-prompt', '--append-system-prompt',
    '--system-prompt-file', '--append-system-prompt-file', '--agent', '--agents',
    '--add-dir', '--output-format', '--input-format', '--json-schema', '--max-turns',
    '--max-budget-usd', '--fallback-model', '--session-id', '--plugin-dir',
    '--betas', '--name', '--thinking', '--max-thinking-tokens',
}
_BOOLEAN_OPTIONS = {
    '--chrome', '--no-chrome', '--verbose', '--debug', '--strict-mcp-config', '--print', '-p',
    '--include-partial-messages', '--disable-slash-commands', '--no-session-persistence',
    '--dangerously-skip-permissions', '--allow-dangerously-skip-permissions',
}
_LAUNCHER_WORDS = {
    'fast', 'base', 'plan', 'opus', 'rich', 'fable', 'ultracode',
    'low', 'medium', 'high', 'xhigh', 'max', 'light', 'bypass', 'acceptEdits', 'dontAsk',
}
# Keywords that choose a model / an effort. Every mode keyword also sets its own
# effort (HARNESS_MODE_EFFORT), so it suppresses the inherited effort as well.
_MODEL_WORDS = {'fast', 'base', 'plan', 'opus', 'rich', 'fable', 'ultracode'}
_EFFORT_WORDS = {'low', 'medium', 'high', 'xhigh', 'max', 'ultracode'}


def _request(args: list[str]) -> dict | None:
    """Recognize direct Claude exact resumes; reject ambiguous fork controls."""
    if not isinstance(args, list) or len(args) > 4096 or any(not isinstance(arg, str) or len(arg) > 65536 for arg in args):
        raise ForkError('invalid_claude_request')
    if args and args[0] in {'codex', 'kiro', 'kiro-cli', 'codex-gateway', 'gateway', 'checkup', 'codex-smoke'}:
        return None
    resumes = []; forks = 0; permission = False; unknown = False; conflict = False
    model = effort = False
    launcher_indices = []; passthrough_index = None
    native = False; i = 0; end = len(args)
    while i < len(args):
        arg = args[i]
        if arg == '--': end = i; break
        if arg == '--passthrough':
            if native: unknown = True
            passthrough_index = i
            native = True; i += 1; continue
        if not native and arg in _LAUNCHER_WORDS:
            launcher_indices.append(i)
            permission |= arg in {'bypass', 'acceptEdits', 'dontAsk'}
            model |= arg in _MODEL_WORDS
            effort |= arg in _EFFORT_WORDS or arg in _MODEL_WORDS
            i += 1; continue
        if arg.startswith('--fork-session='):
            raise ForkError('invalid_claude_fork_flag')
        if arg == '--fork-session': forks += 1; i += 1; continue
        if arg in {'--continue', '-c'} or (not native and arg == 'continue'):
            conflict = True; i += 1; continue
        if arg in {'--resume', '-r'} or (not native and arg == 'resume'):
            value = args[i+1] if i+1 < len(args) else ''
            if value == '--fork-session' or value.startswith('--fork-session='):
                raise ForkError('explicit_claude_fork_requires_uuid')
            resumes.append((i, i+2, value)); i += 2; continue
        if arg.startswith('--resume='):
            resumes.append((i, i+1, arg.partition('=')[2])); i += 1; continue
        option, equals, value = arg.partition('=')
        if option in _VALUE_OPTIONS:
            if not equals:
                if i+1 >= len(args): unknown = True
                else: value = args[i+1]
                i += 2
            else: i += 1
            if option == '--permission-mode':
                if value not in {'default', 'acceptEdits', 'plan', 'auto', 'dontAsk', 'bypassPermissions'}:
                    unknown = True
                else: permission = True
            if option == '--session-id': conflict = True
            model |= option == '--model'
            effort |= option == '--effort'
            continue
        if arg in _BOOLEAN_OPTIONS:
            permission |= arg == '--dangerously-skip-permissions'
            i += 1; continue
        # Native options may follow a positional prompt; only -- ends options.
        if not arg.startswith('-'):
            i += 1; continue
        unknown = True; i += 1
    if forks > 1 or len(resumes) > 1:
        raise ForkError('ambiguous_claude_resume')
    if not resumes:
        if forks: raise ForkError('explicit_claude_fork_requires_uuid')
        return None
    start, stop, value = resumes[0]
    if not UUID.fullmatch(value):
        if forks: raise ForkError('explicit_claude_fork_requires_uuid')
        return None
    if unknown or conflict:
        raise ForkError('unsupported_claude_resume_arguments')
    return {'native_id': value.lower(), 'fork': bool(forks), 'resume_start': start,
            'resume_stop': stop, 'options_end': end, 'permission_explicit': permission,
            '_launcher_indices': launcher_indices, '_passthrough_index': passthrough_index,
            '_model_explicit': model, '_effort_explicit': effort}


def request(args: list[str]) -> dict | None:
    """Return the stable public selection without internal token-role details."""
    selected = _request(args)
    return {key: value for key, value in selected.items() if not key.startswith('_')} if selected else None


def rewrite(args: list[str], receipt: dict, inherited: dict | None = None) -> list[str]:
    """Send proven native controls through passthrough without keyword capture.

    `inherited` (PreparedFork.inherited) is given only for a top-level launch with
    a bound origin record; explicit caller flags and keywords always win.
    """
    selected = _request(args)
    if not selected or not selected['fork'] or receipt.get('native_id') != selected['native_id'] or receipt.get('runtime') != 'claude':
        raise ForkError('fork_request_receipt_mismatch')
    locator = _absolute(receipt.get('locator', ''))
    start, stop = selected['resume_start'], selected['resume_stop']
    launcher_indices = set(selected['_launcher_indices'])
    launcher_args = [args[index] for index in selected['_launcher_indices']]
    native_args = []
    index = 0
    while index <= len(args):
        if index == selected['options_end']:
            if not selected['permission_explicit']:
                native_args.extend(['--permission-mode', (inherited or {}).get('permission') or 'default'])
            if inherited and inherited.get('model') and not selected['_model_explicit']:
                native_args.extend(['--model', inherited['model']])
            if inherited and inherited.get('effort') and not selected['_effort_explicit']:
                native_args.extend(['--effort', inherited['effort']])
        if index == len(args): break
        if index == start:
            native_args.extend(['--resume', str(locator)])
            index = stop
            continue
        if index not in launcher_indices and index != selected['_passthrough_index']:
            native_args.append(args[index])
        index += 1
    # Even a bare fork must bypass the composer. Reuse the launcher's existing
    # caller-wins reconciliation; model/permission values never become keywords.
    return launcher_args + ['--passthrough'] + native_args
