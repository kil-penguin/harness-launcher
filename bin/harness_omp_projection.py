"""Internal OMP configuration publisher/selector, under the trusted config TCB.

Publisher is called by the already authorized root controller, not by native
OMP. Receipt hashes describe provenance; they do not authenticate or grant it.
No provider credentials, network, subprocess, registry write or native call.
"""
import contextlib
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import stat
import sys
import unicodedata
import uuid

LIMIT = 65536
INVALID = 'OMP_PROJECTION_INVALID'
BINDING = 'OMP_BINDING_MISMATCH'
PROVENANCE = 'OMP_PROVENANCE_UNBOUND'


class ProjectionError(Exception):
    def __init__(self, label=INVALID):
        self.label = label
        super().__init__(label)


def require(condition, label=INVALID):
    if not condition:
        raise ProjectionError(label)


def keys(value, expected):
    require(type(value) is dict and set(value) == set(expected.split()))


def integer(value, positive=False):
    require(type(value) is int and value >= (1 if positive else 0))


def text(value):
    require(type(value) is str and 0 < len(value) <= 4096 and
            all(unicodedata.category(c) not in ('Cc', 'Cf', 'Cs') for c in value))
    return value


def sha(value):
    require(type(value) is str and re.fullmatch('[0-9a-f]{64}', value))


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(value).hexdigest()


def pairs(items):
    value = {}
    for key, item in items:
        require(key not in value)
        value[key] = item
    return value


def decode(raw):
    require(len(raw) <= LIMIT)
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                          parse_constant=lambda _: require(False))
    except (ValueError, UnicodeError, RecursionError, TypeError):
        raise ProjectionError() from None


def physical(path):
    p = Path(text(str(path)))
    require(p.is_absolute() and str(p) == str(path), BINDING)
    for node in (p, *p.parents):
        require(not node.is_symlink(), BINDING)
    require(p.resolve() == p, BINDING)
    return p


def identity(meta):
    return {'dev': meta.st_dev, 'ino': meta.st_ino, 'uid': meta.st_uid}


def check_identity(value):
    keys(value, 'dev ino uid')
    for item in value.values():
        integer(item)


def stamp(meta):
    return (meta.st_dev, meta.st_ino, meta.st_uid, meta.st_mode,
            meta.st_nlink, meta.st_size, meta.st_mtime_ns, meta.st_ctime_ns)


def directory(path, mode=None):
    p = physical(path)
    meta = p.lstat()
    require(stat.S_ISDIR(meta.st_mode) and meta.st_uid == os.getuid(), BINDING)
    if mode is not None:
        require(stat.S_IMODE(meta.st_mode) == mode, BINDING)
    return identity(meta)


def read_file(path, mode=0o600, limit=LIMIT):
    """Stable, bounded, nonblocking, no-follow read; identities are not grants."""
    p = physical(path)
    try:
        before = p.lstat()
        fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        raise ProjectionError('OMP_PROJECTION_MISSING') from None
    with os.fdopen(fd, 'rb') as stream:
        meta = os.fstat(stream.fileno())
        require(stamp(meta) == stamp(before) and stat.S_ISREG(meta.st_mode) and
                meta.st_uid == os.getuid() and meta.st_nlink == 1 and
                meta.st_size <= limit, BINDING)
        if mode is not None:
            require(stat.S_IMODE(meta.st_mode) == mode, BINDING)
        else:
            require(not (stat.S_IMODE(meta.st_mode) & 0o022), BINDING)
        data = stream.read(limit + 1)
        require(len(data) <= limit and stamp(meta) == stamp(os.fstat(stream.fileno()))
                and stamp(meta) == stamp(p.lstat()), BINDING)
    return data, stamp(meta)


def file_sha(path, mode):
    p = physical(path)
    before = p.lstat()
    require(stat.S_ISREG(before.st_mode) and before.st_uid == os.getuid() and
            before.st_nlink == 1 and stat.S_IMODE(before.st_mode) == mode, BINDING)
    h = hashlib.sha256()
    fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        require(stamp(before) == stamp(os.fstat(stream.fileno())), BINDING)
        for block in iter(lambda: stream.read(1048576), b''):
            h.update(block)
        require(stamp(before) == stamp(os.fstat(stream.fileno())) and
                stamp(before) == stamp(p.lstat()), BINDING)
    return h.hexdigest(), before


def base():
    # Same trusted user HOME as existing profile registration. No additional
    # XDG/cwd/project/OMP environment override is introduced.
    return physical(Path.home()) / '.config/harness-launcher/omp-projection-v1'


def registry_dir():
    return physical(Path.home()) / '.config/harness-launcher/profiles'


def host(installation_id):
    require(type(installation_id) is str and re.fullmatch('[0-9a-f]{32}', installation_id))
    return {'installation_id': installation_id, 'hostname': platform.node(),
            'system': platform.system(), 'architecture': platform.machine(),
            'uid': os.getuid(), 'profile_registry_directory_identity': directory(registry_dir())}


def check_host(value):
    keys(value, 'installation_id hostname system architecture uid profile_registry_directory_identity')
    integer(value['uid'])
    check_identity(value['profile_registry_directory_identity'])
    require(value == host(value['installation_id']), BINDING)


def profile_binding(name, root):
    require(type(name) is str and re.fullmatch('[A-Za-z_][A-Za-z0-9_-]{0,63}', name), BINDING)
    root = physical(root)
    root_id = directory(root)
    path = registry_dir() / name
    raw, meta = read_file(path, None, 8192)
    try:
        require(raw.decode('utf-8').strip() == str(root), BINDING)
    except UnicodeError:
        raise ProjectionError(BINDING) from None
    return {'name': name, 'root': str(root), 'root_identity': root_id,
            'registry': {'path': str(path), 'sha256': digest(raw),
                         'identity': {'dev': meta[0], 'ino': meta[1], 'uid': meta[2]}}}


def check_profile(value):
    keys(value, 'name root root_identity registry')
    text(value['name']); text(value['root']); check_identity(value['root_identity'])
    keys(value['registry'], 'path sha256 identity')
    text(value['registry']['path']); sha(value['registry']['sha256'])
    check_identity(value['registry']['identity'])


def expiry(value):
    require(type(value) is str and re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ', value))
    try:
        parsed = datetime.datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        raise ProjectionError() from None
    return parsed


def fresh(value):
    require(expiry(value) > datetime.datetime.now(datetime.timezone.utc), 'OMP_PROJECTION_EXPIRED')


def check_provenance(value, lane=None, valid_until=None):
    keys(value, 'binding_sha256 review_sha256 selection_sha256 operation_sha256 controller scope ssot')
    for key in ('binding_sha256', 'review_sha256', 'selection_sha256', 'operation_sha256'):
        sha(value[key])
        require(value[key] != '0' * 64, PROVENANCE)
    keys(value['controller'], 'native pane managed')
    for item in value['controller'].values():
        text(item)
    require(value['scope'] == 'OMP3_CONFIGURATION', PROVENANCE)
    if value['ssot'] is not None:
        ssot = value['ssot']
        keys(ssot, 'revision_sha256 export_sha256 acl_sha256 valid_until')
        for key in ('revision_sha256', 'export_sha256', 'acl_sha256'):
            sha(ssot[key]); require(ssot[key] != '0' * 64, 'OMP_REVISION_UNBOUND')
        expiry(ssot['valid_until'])
    if lane == 'SSOT_ACTIVE':
        require(value['ssot'] is not None, 'OMP_REVISION_UNBOUND')
        fresh(value['ssot']['valid_until'])
        require(expiry(valid_until) <= expiry(value['ssot']['valid_until']), 'OMP_REVISION_UNBOUND')
    if lane == 'BOOTSTRAP_ACTIVE':
        require(value['ssot'] is None, PROVENANCE)


def launcher_binding(consumer):
    runtime = physical(Path.home()) / '.local/state/kilhyeonjun-harness/launcher-runtimes'
    raw, _ = read_file(runtime / 'current.json')
    current = decode(raw)
    keys(current, 'version revision path manifest_sha256')
    require(type(current['revision']) is str and re.fullmatch('[0-9a-f]{40}', current['revision']), BINDING)
    require(type(current['version']) is str and re.fullmatch(r'\d+\.\d+\.\d+', current['version']), BINDING)
    sha(current['manifest_sha256'])
    snapshot = physical(current['path'])
    require(snapshot == runtime / current['revision'] and physical(consumer) == snapshot / 'bin/aliases.zsh', BINDING)
    manifest_raw, _ = read_file(snapshot / 'manifest.json', 0o400, 131072)
    require(digest(manifest_raw) == current['manifest_sha256'], BINDING)
    # Manifests exceed the data-record limit; still bounded and duplicate-safe.
    try:
        manifest = json.loads(manifest_raw.decode('utf-8'), object_pairs_hook=pairs)
    except (ValueError, UnicodeError, RecursionError):
        raise ProjectionError(BINDING) from None
    keys(manifest, 'version revision files')
    require(manifest['revision'] == current['revision'] and manifest['version'] == current['version'] and type(manifest['files']) is dict, BINDING)
    consumer_sha, _ = file_sha(consumer, stat.S_IMODE(Path(consumer).lstat().st_mode))
    for name in ('bin/aliases.zsh', 'bin/harness_omp_projection.py'):
        item = manifest['files'].get(name)
        keys(item, 'mode sha256'); integer(item['mode']); sha(item['sha256'])
        require(item['mode'] in (0o400, 0o500), BINDING)
        actual, _ = file_sha(snapshot / name, item['mode'])
        require(actual == item['sha256'], BINDING)
    return {'current_sha256': digest(raw), 'revision': current['revision'],
            'manifest_sha256': current['manifest_sha256'], 'consumer_sha256': consumer_sha}


def binary_binding(path, release):
    keys(release, 'version source_revision')
    text(release['version'])
    require(type(release['source_revision']) is str and re.fullmatch('[0-9a-f]{40}', release['source_revision']))
    h, meta = file_sha(path, 0o555)
    return {'path': str(path), 'sha256': h, 'bytes': meta.st_size,
            'uid': meta.st_uid, 'mode': 0o555, 'single_link': True, 'release': release}


def namespace_binding(path, binary):
    p = physical(path)
    root_id = directory(p, 0o700)
    for name in ('home', 'agent', 'history', 'cwd', 'tmp', 'cache', 'config', 'state', 'data', 'native-cache'):
        directory(p / name, 0o700)
    raw, _ = read_file(p / '.owner.json')
    owner = decode(raw)
    require(type(owner) is dict and owner.get('schema') == 'p0-normal-omp-owner-v1' and
            owner.get('prefix') == str(p) and owner.get('binary') == binary['path'] and
            owner.get('asset_sha256') == binary['sha256'] and type(owner.get('identity')) is dict, BINDING)
    for key, value in owner['identity'].items():
        text(key); text(value)
    return {'path': str(p), 'root_identity': root_id, 'mode': 0o700,
            'owner_marker': {'path': str(p / '.owner.json'), 'sha256': digest(raw),
                             'mode': 0o600, 'identity': owner['identity']}}


def check_row(row):
    common = 'profile_binding enrollment_generation lane bootstrap_retired valid_until provenance'
    require(type(row) is dict)
    require(row.get('lane') in ('BOOTSTRAP_ACTIVE', 'TRANSITION_BLOCKED', 'SSOT_ACTIVE', 'REVOKED'))
    active = row['lane'] in ('BOOTSTRAP_ACTIVE', 'SSOT_ACTIVE')
    keys(row, common + (' launcher binary native_home' if active else ' reason'))
    check_profile(row['profile_binding']); integer(row['enrollment_generation'], True)
    require(type(row['bootstrap_retired']) is bool); expiry(row['valid_until'])
    check_provenance(row['provenance'])
    if not active:
        text(row['reason']); return
    require(not (row['lane'] == 'BOOTSTRAP_ACTIVE' and row['bootstrap_retired']))
    require(row['lane'] != 'SSOT_ACTIVE' or row['bootstrap_retired'])
    launcher = row['launcher']; keys(launcher, 'current_sha256 revision manifest_sha256 consumer_sha256')
    for key in ('current_sha256', 'manifest_sha256', 'consumer_sha256'): sha(launcher[key])
    require(type(launcher['revision']) is str and re.fullmatch('[0-9a-f]{40}', launcher['revision']))
    binary = row['binary']; keys(binary, 'path sha256 bytes uid mode single_link release')
    text(binary['path']); sha(binary['sha256']); integer(binary['bytes'], True); integer(binary['uid']); integer(binary['mode'])
    require(binary['mode'] == 0o555 and binary['single_link'] is True)
    keys(binary['release'], 'version source_revision'); text(binary['release']['version'])
    require(type(binary['release']['source_revision']) is str and re.fullmatch('[0-9a-f]{40}', binary['release']['source_revision']))
    ns = row['native_home']; keys(ns, 'path root_identity mode owner_marker')
    text(ns['path']); check_identity(ns['root_identity']); integer(ns['mode']); require(ns['mode'] == 0o700)
    marker = ns['owner_marker']; keys(marker, 'path sha256 mode identity')
    text(marker['path']); sha(marker['sha256']); integer(marker['mode']); require(marker['mode'] == 0o600 and type(marker['identity']) is dict)
    for key, value in marker['identity'].items(): text(key); text(value)


def capture_row(name, root, consumer, binary, namespace, valid_until, release,
                provenance, lane='BOOTSTRAP_ACTIVE', bootstrap_retired=False):
    """Root-owned capture of nonsecret config facts, never native execution."""
    fresh(valid_until); check_provenance(provenance, lane, valid_until)
    row = {'profile_binding': profile_binding(name, root), 'enrollment_generation': 1,
           'lane': lane, 'bootstrap_retired': bootstrap_retired, 'valid_until': valid_until,
           'provenance': provenance, 'launcher': launcher_binding(consumer),
           'binary': binary_binding(binary, release)}
    row['native_home'] = namespace_binding(namespace, row['binary'])
    check_row(row)
    return row


def validate_selected(row, name, root, consumer):
    check_row(row)
    require(row['lane'] in ('BOOTSTRAP_ACTIVE', 'SSOT_ACTIVE'), 'OMP_PROJECTION_REVOKED')
    fresh(row['valid_until']); check_provenance(row['provenance'], row['lane'], row['valid_until'])
    require(row['profile_binding'] == profile_binding(name, root), BINDING)
    require(row['launcher'] == launcher_binding(consumer), BINDING)
    require(row['binary'] == binary_binding(row['binary']['path'], row['binary']['release']), BINDING)
    require(row['native_home'] == namespace_binding(row['native_home']['path'], row['binary']), BINDING)


def check_index(value):
    keys(value, 'schema_version generation complete_enrollment_set host provenance enrolled_profiles')
    integer(value['schema_version']); require(value['schema_version'] == 1)
    integer(value['generation'], True); require(value['complete_enrollment_set'] is True)
    check_host(value['host']); check_provenance(value['provenance'])
    require(type(value['enrolled_profiles']) is dict)
    for name, entry in value['enrolled_profiles'].items():
        keys(entry, 'profile_binding enrollment_generation selection disposition legacy_retired payload_row_sha256 payload_generation selection_receipt')
        check_profile(entry['profile_binding']); require(entry['profile_binding']['name'] == name)
        integer(entry['enrollment_generation'], True); integer(entry['payload_generation'], True)
        require(entry['selection'] == 'ENROLLED' and entry['disposition'] in ('ACTIVE', 'BLOCKED', 'REVOKED'))
        require(type(entry['legacy_retired']) is bool); sha(entry['payload_row_sha256'])
        check_provenance(entry['selection_receipt'])


def check_state(value, index):
    keys(value, 'schema_version generation host profiles')
    integer(value['schema_version']); require(value['schema_version'] == 1)
    integer(value['generation'], True); check_host(value['host'])
    require(value['host'] == index['host'] and type(value['profiles']) is dict, BINDING)
    for name, row in value['profiles'].items():
        check_row(row); require(name == row['profile_binding']['name'])


def select(name, root, consumer):
    """None means proven never-enrolled; every unavailable/error is a STOP."""
    try:
        directory(base(), 0o700)
        i1, identity1 = read_file(base() / 'enrollment.json')
        index = decode(i1); check_index(index)
        entry = index['enrolled_profiles'].get(name)
        result = None
        if entry is not None:
            require(entry['profile_binding'] == profile_binding(name, root), BINDING)
            require(entry['disposition'] == 'ACTIVE', 'OMP_PROJECTION_REVOKED')
            raw, _ = read_file(base() / 'state.json')
            state = decode(raw); check_state(state, index)
            row = state['profiles'].get(name)
            require(row is not None and state['generation'] == entry['payload_generation'] and
                    digest(canonical(row)) == entry['payload_row_sha256'] and
                    row['enrollment_generation'] == entry['enrollment_generation'] and
                    row['provenance'] == entry['selection_receipt'], BINDING)
            validate_selected(row, name, root, consumer)
            result = {'HARNESS_OMP_BIN': row['binary']['path'],
                      'HARNESS_OMP_ROOT': row['native_home']['path'],
                      'HARNESS_OMP_SHA256': row['binary']['sha256']}
        i2, identity2 = read_file(base() / 'enrollment.json')
        require(i1 == i2 and identity1 == identity2, BINDING)
        return result
    except ProjectionError:
        raise
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        raise ProjectionError() from None


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try: os.fsync(fd)
    finally: os.close(fd)


def atomic_json(path, value):
    raw = canonical(value); require(len(raw) <= LIMIT)
    path = physical(path); directory(path.parent, 0o700)
    if path.exists() or path.is_symlink(): read_file(path)
    temp = path.with_name('.' + uuid.uuid4().hex + '.tmp')
    fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        os.replace(temp, path); sync_directory(path.parent)
    finally:
        temp.unlink(missing_ok=True)


class Publisher:
    """Internal trusted-controller API. No authentication or native grant API.

    prepare is first-rollout-only. publish/block require a previously read
    token (full index+state SHA and generation) under one cooperative lock.
    """
    def __init__(self):
        self.root = base()
        self.index = self.root / 'enrollment.json'
        self.state = self.root / 'state.json'
        self.lock = self.root / 'writer.lock'

    @contextlib.contextmanager
    def locked(self):
        directory(self.root, 0o700)
        read_file(self.lock)
        fd = os.open(self.lock, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            before = stamp(os.fstat(fd)); require(before == stamp(self.lock.lstat()), BINDING)
            try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError: raise ProjectionError() from None
            require(before == stamp(self.lock.lstat()), BINDING)
            yield
            require(before == stamp(self.lock.lstat()), BINDING)
        finally: os.close(fd)

    def prepare(self, provenance):
        """Explicit first rollout only, before enabling the new selector.

        Existing directory (even corrupt/missing index) cannot be reset empty.
        Failed initialization deliberately requires root disposition.
        """
        try:
            check_provenance(provenance)
            directory(registry_dir())
            parent = self.root.parent
            if not parent.exists(): parent.mkdir(mode=0o700)
            directory(parent)
            self.root.mkdir(mode=0o700)  # exclusive first creation, no reset
            fd = os.open(self.lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            os.fsync(fd); os.close(fd); sync_directory(self.root)
            with self.locked():
                value = {'schema_version': 1, 'generation': 1, 'complete_enrollment_set': True,
                         'host': host(uuid.uuid4().hex), 'provenance': provenance, 'enrolled_profiles': {}}
                # Under exclusive new directory+lock; no previous index exists.
                require(not self.index.exists() and not self.index.is_symlink())
                atomic_json(self.index, value)
            return self.token()
        except ProjectionError: raise
        except (OSError, ValueError, TypeError): raise ProjectionError() from None

    def records(self):
        directory(self.root, 0o700)
        raw, _ = read_file(self.index); index = decode(raw); check_index(index)
        if self.state.exists() or self.state.is_symlink():
            data, _ = read_file(self.state); state = decode(data); check_state(state, index)
        else:
            require(not index['enrolled_profiles'], 'OMP_PROJECTION_MISSING')
            data = None
            state = {'schema_version': 1, 'generation': 0, 'host': index['host'], 'profiles': {}}
        # A failed pair is not a fresh publisher base. Do not silently adopt
        # uncommitted rows, stale joins or missing retained tombstones.
        require(set(state['profiles']) == set(index['enrolled_profiles']), BINDING)
        for name, entry in index['enrolled_profiles'].items():
            row = state['profiles'][name]
            require(entry['profile_binding'] == row['profile_binding'] and
                    entry['payload_generation'] == state['generation'] and
                    entry['payload_row_sha256'] == digest(canonical(row)) and
                    entry['enrollment_generation'] == row['enrollment_generation'], BINDING)
        token = {'index_sha256': digest(raw), 'index_generation': index['generation'],
                 'state_sha256': digest(data) if data is not None else None, 'state_generation': state['generation']}
        return index, state, token

    def token(self):
        try: return self.records()[2]
        except ProjectionError: raise
        except (OSError, ValueError, TypeError): raise ProjectionError() from None

    def check_token(self, expected):
        keys(expected, 'index_sha256 index_generation state_sha256 state_generation')
        sha(expected['index_sha256']); integer(expected['index_generation'], True)
        integer(expected['state_generation'])
        if expected['state_sha256'] is None:
            require(expected['state_generation'] == 0)
        else:
            sha(expected['state_sha256']); integer(expected['state_generation'], True)

    def commit_pair(self, index, state):
        # All profile joins move together when the shared payload generation
        # advances; unrelated enrolled profiles remain usable after success.
        state['generation'] += 1; index['generation'] += 1
        for name, entry in index['enrolled_profiles'].items():
            require(name in state['profiles'])
            entry['payload_generation'] = state['generation']
            entry['payload_row_sha256'] = digest(canonical(state['profiles'][name]))
        # Any failure between writes is an explicit join STOP, not fallback.
        atomic_json(self.state, state)
        atomic_json(self.index, index)

    def publish(self, name, row, expected, legacy_retired=None):
        try:
            self.check_token(expected)
            row = decode(canonical(row))
            with self.locked():
                index, state, token = self.records(); require(token == expected)
                check_row(row)
                validate_selected(row, name, row['profile_binding']['root'],
                                  str(physical(Path.home()) / '.local/state/kilhyeonjun-harness/launcher-runtimes' / row['launcher']['revision'] / 'bin/aliases.zsh'))
                previous = index['enrolled_profiles'].get(name)
                prior_row = state['profiles'].get(name)
                if previous:
                    require(previous['disposition'] != 'REVOKED', 'OMP_PROJECTION_REVOKED')
                    require(previous['profile_binding'] == row['profile_binding'], BINDING)
                    require(not prior_row['bootstrap_retired'] or row['bootstrap_retired'])
                    require(not prior_row['bootstrap_retired'] or row['lane'] == 'SSOT_ACTIVE')
                    require(row['lane'] != 'SSOT_ACTIVE' or previous['disposition'] == 'BLOCKED')
                    if legacy_retired is None: legacy_retired = previous['legacy_retired']
                    require(not previous['legacy_retired'] or legacy_retired is True)
                    generation = previous['enrollment_generation'] + 1
                else:
                    require(row['lane'] == 'BOOTSTRAP_ACTIVE' and not row['bootstrap_retired'])
                    generation = 1
                    if legacy_retired is None: legacy_retired = False
                require(type(legacy_retired) is bool)
                require(not legacy_retired or row['lane'] == 'SSOT_ACTIVE')
                row['enrollment_generation'] = generation
                state['profiles'][name] = row
                index['enrolled_profiles'][name] = {
                    'profile_binding': row['profile_binding'], 'enrollment_generation': generation,
                    'selection': 'ENROLLED', 'disposition': 'ACTIVE', 'legacy_retired': legacy_retired,
                    'payload_row_sha256': digest(canonical(row)), 'payload_generation': state['generation'] + 1,
                    'selection_receipt': row['provenance']}
                index['provenance'] = row['provenance']
                self.commit_pair(index, state)
            return self.token()
        except ProjectionError: raise
        except (OSError, ValueError, TypeError, KeyError): raise ProjectionError() from None

    def block(self, name, expected, provenance, reason, revoke=False):
        try:
            self.check_token(expected)
            check_provenance(provenance); text(reason); require(type(revoke) is bool)
            with self.locked():
                index, state, token = self.records(); require(token == expected)
                require(name in index['enrolled_profiles'])
                entry = index['enrolled_profiles'][name]; prior = state['profiles'][name]
                require(entry['disposition'] != 'REVOKED', 'OMP_PROJECTION_REVOKED')
                entry['disposition'] = 'REVOKED' if revoke else 'BLOCKED'
                entry['enrollment_generation'] += 1
                entry['selection_receipt'] = provenance
                index['provenance'] = provenance; index['generation'] += 1
                # Critical: block selector BEFORE touching/removing active data.
                atomic_json(self.index, index)
                state['profiles'][name] = {
                    'profile_binding': prior['profile_binding'],
                    'enrollment_generation': entry['enrollment_generation'],
                    'lane': 'REVOKED' if revoke else 'TRANSITION_BLOCKED',
                    'bootstrap_retired': prior['bootstrap_retired'],
                    'valid_until': prior['valid_until'], 'provenance': provenance, 'reason': reason}
                self.commit_pair(index, state)
            return self.token()
        except ProjectionError: raise
        except (OSError, ValueError, TypeError, KeyError): raise ProjectionError() from None


def main(argv):
    # Private shell bridge is readonly. All writes require the root controller
    # to call Publisher directly; no public user-facing command is registered.
    try:
        require(len(argv) == 5 and argv[1] == 'select')
        result = select(argv[2], argv[3], argv[4])
        # Paths are validated control-free; the shell reads fields without eval.
        if result is None:
            print('NEVER_ENROLLED')
        else:
            print('ENROLLED')
            for key in ('HARNESS_OMP_BIN', 'HARNESS_OMP_ROOT', 'HARNESS_OMP_SHA256'):
                print(result[key])
        return 0
    except ProjectionError as error:
        print('harness-launcher: ' + error.label, file=sys.stderr)
        return 2
    except (OSError, ValueError, TypeError, KeyError):
        print('harness-launcher: ' + INVALID, file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
