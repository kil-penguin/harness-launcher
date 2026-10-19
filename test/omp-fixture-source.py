"""Portable test admission: validate bytes/metadata, never import or spawn payload.

The reviewed helper pins a repository-relative synthetic test policy. The fixture
manifest has the same bytes as that policy. Update payload records, both JSON
files, and POLICY_SHA256 together; none of these inputs grants runtime authority.
"""
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

POLICY_SHA256 = '7669649f4d2908322cfa4ad794a8cb2dca75f4cf85a8d61ec5c9796c8cb17349'
CODE = {
    'core/scripts/harness_compile.py', 'core/scripts/omp_required_extension.ts',
    'core/bin/harness-python', 'core/hooks/pre-bash-curl-block.sh',
}
YAML = {'.claude/source/' + name + '.yaml' for name in (
    'runtime-contract', 'budgets', 'settings', 'agents', 'commands', 'skills', 'claude-md')}
LIMIT = 1024 * 1024


class Refused(Exception):
    pass


def require(ok, reason):
    if not ok:
        raise Refused(reason)


def identity(s):
    return (s.st_dev, s.st_ino, s.st_mode, s.st_uid, s.st_gid,
            s.st_nlink, s.st_size, s.st_mtime_ns, s.st_ctime_ns)


def directory(path, owner=True, sticky_ancestor=False):
    s = path.lstat()
    require(stat.S_ISDIR(s.st_mode), 'nonphysical directory')
    require(s.st_uid == os.getuid() if owner else s.st_uid in (0, os.getuid()),
            'foreign directory owner')
    require(not s.st_mode & 0o022 or (sticky_ancestor and s.st_uid == 0 and
                                     s.st_mode & stat.S_ISVTX), 'writable directory')
    return identity(s)


def read_file(path, mode=None):
    before = path.lstat()
    require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1,
            'not regular singlelink')
    require(before.st_uid == os.getuid(), 'foreign file owner')
    require(not before.st_mode & 0o022, 'writable file')
    if mode is not None:
        require(stat.S_IMODE(before.st_mode) == mode, 'wrong file mode')
    require(before.st_size <= LIMIT, 'file too large')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        require(identity(os.fstat(stream.fileno())) == identity(before), 'file changed before read')
        data = stream.read(LIMIT + 1)
        require(len(data) <= LIMIT, 'file grew')
        require(identity(os.fstat(stream.fileno())) == identity(before), 'file changed during read')
    require(identity(path.lstat()) == identity(before), 'file changed after read')
    return data, identity(before)


def unique_object(pairs):
    obj = {}
    for key, value in pairs:
        require(key not in obj, 'duplicate manifest key')
        obj[key] = value
    return obj


def admit():
    require(len(sys.argv) == 1, 'unsupported arguments')
    here = Path(__file__).absolute().parent
    checkout = here.parent
    raw = os.environ.get('HARNESS_TEST_OMP_SOURCE', str(here / 'fixtures/w07-source'))
    root = Path(raw)
    require(raw and root.is_absolute() and str(root) == raw and '..' not in root.parts,
            'root must be an absolute physical path')
    ancestors = {}
    for anchor in (checkout, root):
        for path in reversed((anchor, *anchor.parents)):
            # Ordinary owned checkouts may be under the system sticky TMP root.
            # This exception is only above an owned anchor, never on/below it.
            ancestors[path] = directory(path, owner=(path == anchor),
                                        sticky_ancestor=(path != anchor))
        require(anchor.resolve(strict=True) == anchor, 'nonphysical root')
    ancestors[here] = directory(here)
    # Trusted policy is in this checkout, never selected by the supplied root.
    _, helper_marker = read_file(here / 'omp-fixture-source.py', 0o644)
    policy_path = here / 'omp-fixture-policy.json'
    policy_bytes, policy_marker = read_file(policy_path, 0o644)
    require(hashlib.sha256(policy_bytes).hexdigest() == POLICY_SHA256, 'trusted policy changed')
    policy = json.loads(policy_bytes, object_pairs_hook=unique_object)
    require(set(policy) == {'schema', 'origin', 'revision', 'files'} and
            policy['schema'] == 'w07.synthetic-test-source.v1' and
            policy['origin'] == 'w07-portable-synthetic-fixture' and
            policy['revision'] == '281437a9e276d00fbe83e14eed7027e1196ff8b4',
            'test source identity mismatch')
    expected = {row['path']: row for row in policy['files']}
    require(set(expected) == CODE | YAML and len(policy['files']) == 11,
            'wrong fixed allowlist')
    directories = {'.'}
    for name in expected:
        directories.update(str(x) for x in Path(name).parents)
    observed_files, observed_dirs, observed_ids = set(), {'.'}, {}
    for parent, dirs, files in os.walk(root, followlinks=False):
        require(len(observed_ids) < 100, 'tree exceeds bound')
        for name in dirs + files:
            path = Path(parent) / name
            rel = str(path.relative_to(root))
            require(rel in directories or rel in expected or rel == 'manifest.json', 'extra payload')
            s = path.lstat()
            if name in dirs:
                observed_ids[path] = directory(path)
                observed_dirs.add(rel)
            else:
                observed_files.add(rel)
                require(stat.S_ISREG(s.st_mode), 'nonphysical file')
    require(observed_files == set(expected) | {'manifest.json'} and observed_dirs == directories,
            'missing or extra payload')
    manifest_bytes, marker = read_file(root / 'manifest.json', 0o644)
    observed_ids[root / 'manifest.json'] = marker
    manifest = json.loads(manifest_bytes, object_pairs_hook=unique_object)
    require(manifest_bytes == policy_bytes and manifest == policy, 'manifest/source identity mismatch')
    for name, row in expected.items():
        content, marker = read_file(root / name, int(row['mode'], 8))
        require(len(content) == row['bytes'] and hashlib.sha256(content).hexdigest() == row['sha256'],
                'payload digest mismatch')
        observed_ids[root / name] = marker
    observed_ids[here / 'omp-fixture-source.py'] = helper_marker
    observed_ids[policy_path] = policy_marker
    for path, marker in {**ancestors, **observed_ids}.items():
        require(identity(path.lstat()) == marker, 'tree changed during admission')
    return str(root)


if __name__ == '__main__':
    try:
        result = admit()
    except (Refused, OSError, ValueError, KeyError, TypeError) as error:
        # Reasons are metadata only; never include payload content or traceback.
        reason = str(error) if isinstance(error, Refused) else type(error).__name__
        print(json.dumps({'status': 'REFUSED', 'reason': reason}), file=sys.stderr)
        sys.exit(2)
    print(result)
