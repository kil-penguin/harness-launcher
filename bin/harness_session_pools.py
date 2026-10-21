"""Source-bound profile pools; existing absolute session roots never move."""
import json
import os
import uuid
from pathlib import Path
import re
import stat
import importlib.util

POOL_API = 1
UUID = re.compile(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}')
RESERVED = {'sessions', 'worktrees', 'native-history', 'archives', 'diagnostics', 'maintenance', 'gc'}
_spec = importlib.util.spec_from_file_location(__name__+'_resolver', Path(__file__).with_name('harness_profile_resolver.py'))
_resolver = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_resolver)


class PoolError(ValueError):
    pass


def check_path(path):
    path = Path(path).absolute()
    if '..' in path.parts:
        raise PoolError('unsafe pool path')
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise PoolError('redirected pool path')


def read_scalar(path):
    check_path(Path(path).parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                or info.st_mode & 0o022 or info.st_size > 65536):
            raise PoolError('unsafe pool metadata')
        raw = stream.read(65537)
        if len(raw) > 65536:
            raise PoolError('pool metadata limit')
    value = raw.decode().removesuffix('\n')
    if not value or any(ord(c) < 32 for c in value):
        raise PoolError('invalid pool metadata')
    return value


def registered_source(source, registry=None):
    registry = Path(registry or Path.home()/'.config/harness-launcher/profiles')
    check_path(registry)
    source = Path(source).resolve(strict=True)
    matches = [name for name, root in _resolver.registered_profiles(registry) if root == source]
    if len(matches) != 1 or matches[0] in RESERVED:
        raise PoolError('source profile is absent, ambiguous or reserved')
    # Re-read the selected registration without following its final symlink.
    if Path(read_scalar(registry/matches[0])).resolve(strict=True) != source:
        raise PoolError('source registration changed')
    return matches[0]


def profile_state(base, source, registry=None):
    result = Path(base)/registered_source(source, registry)
    check_path(result)
    return result


def source_pools(base, source, registry=None):
    base = Path(base)
    return [profile_state(base, source, registry), base]


def registered_pools(base, registry=None):
    registry = Path(registry or Path.home()/'.config/harness-launcher/profiles')
    check_path(registry)
    return list(dict.fromkeys([Path(base), *[profile_state(base, root, registry)
                         for _, root in _resolver.registered_profiles(registry)]]))


def registered_sources(registry=None):
    registry=Path(registry or Path.home()/'.config/harness-launcher/profiles')
    check_path(registry)
    rows=_resolver.registered_profiles(registry)
    return [(registered_source(root,registry),root) for _,root in rows]


def find_owner(base, source, owner, registry=None):
    if not UUID.fullmatch(owner):
        raise PoolError('invalid session UUID')
    matches = []
    for pool in source_pools(base, source, registry):
        records = pool/'sessions';check_path(records)
        try:
            entries = list(records.iterdir())
        except FileNotFoundError:
            continue
        for record in entries:
            if record.name.lower() != owner.lower():
                continue
            check_path(record)
            if (read_scalar(record/'source-root') != str(Path(source).resolve(strict=True))
                    or read_scalar(record/'session-root') != str(pool/'worktrees'/record.name)):
                raise PoolError('session source or absolute root mismatch')
            matches.append((pool, record.name))
    if len(matches) != 1:
        raise PoolError('session owner is absent or ambiguous')
    return matches[0]


def initialize_pool(base, source, registry=None):
    pool = profile_state(base, source, registry)
    pool.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = pool.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PoolError('profile pool is not private')
    expected = json.dumps({'schema': 1, 'profile': pool.name, 'source_root': str(Path(source).resolve()),
                           'pool_base': str(Path(base))}, sort_keys=True, separators=(',', ':'))
    marker = pool/'profile-state.json'
    temporary = pool/('.profile-state-'+uuid.uuid4().hex)
    try:
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(expected+'\n');stream.flush();os.fsync(stream.fileno())
        try:os.link(temporary,marker,follow_symlinks=False)
        except FileExistsError:
            if read_scalar(marker) != expected:
                raise PoolError('profile pool identity mismatch')
        temporary.unlink()
        directory=os.open(pool,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:temporary.unlink(missing_ok=True)
    return pool


def binding_home(state, source, registry=None):
    state = Path(state)
    marker = state/'profile-state.json'
    if not os.path.lexists(marker):
        return state.parent
    value = json.loads(read_scalar(marker))
    expected = {'schema': 1, 'profile': state.name, 'source_root': str(Path(source).resolve()),
                'pool_base': str(state.parent)}
    if value != expected or profile_state(state.parent, source, registry) != state:
        raise PoolError('profile binding home mismatch')
    return state.parent.parent
