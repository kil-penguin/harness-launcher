#!/usr/bin/env python3
"""Bounded metadata-only observations. Never call GC, archive or native helpers."""
import argparse
from contextlib import contextmanager
import datetime as dt
import errno
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import sys
import time

UUID = re.compile(r'[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}')
OID = re.compile(r'(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})')
STATES = {'OPEN', 'SUBMITTED', 'INTEGRATING', 'DELIVERED', 'ABANDONED', 'CONFLICT', 'CLOSED', 'DISCARDED'}
TERMINAL = {'CLOSED', 'DELIVERED', 'DISCARDED'}
READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
DIR_FLAGS = READ_FLAGS | os.O_DIRECTORY
MAX_METADATA = 65536


class Unknown(ValueError):
    """Missing, unsafe, changing or inaccessible metadata is never absence."""


class Deadline(Unknown):
    """The cooperative deadline elapsed; remaining observations stay unknown."""


def check_deadline(budget):
    if budget is not None and not budget.time_available():
        raise Deadline('scan_budget')


def parent_device(root):
    return os.stat(Path(root).parent).st_dev


def root_identity_matches(recorded, info, root):
    """Recorded "<dev> <ino>" names the opened root: same inode and the same device,
    or a whole-volume device renumbering with the root on its worktrees device."""
    fields = recorded.split()
    if len(fields) != 2 or not all(field.isascii() and field.isdigit() for field in fields):
        return False
    device, inode = int(fields[0]), int(fields[1])
    if info.st_ino != inode:
        return False
    return info.st_dev == device or parent_device(root) == info.st_dev


@contextmanager
def directory(path, budget=None):
    """Open every component without following symlinks, including ancestors."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise Unknown('unsafe_path')
    check_deadline(budget)
    fd = os.open('/', DIR_FLAGS)
    try:
        for part in path.parts[1:]:
            check_deadline(budget)
            child = os.open(part, DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def owned_regular(info):
    return (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
            and info.st_nlink == 1 and not info.st_mode & 0o022)


def stamp(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def metadata(path, budget=None):
    check_deadline(budget)
    with directory(path.parent, budget) as parent:
        check_deadline(budget)
        fd = os.open(path.name, READ_FLAGS, dir_fd=parent)
        try:
            check_deadline(budget)
            before = os.fstat(fd)
            if not owned_regular(before) or before.st_size > MAX_METADATA:
                raise Unknown('unsafe_metadata')
            data = b''
            while len(data) <= MAX_METADATA:
                check_deadline(budget)
                chunk = os.read(fd, min(8192, MAX_METADATA + 1 - len(data)))
                if not chunk:
                    break
                data += chunk
            check_deadline(budget)
            after = os.fstat(fd)
            check_deadline(budget)
            entry = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            if len(data) > MAX_METADATA or stamp(before) != stamp(after) or stamp(after) != stamp(entry):
                raise Unknown('changing_metadata')
            return data
        finally:
            os.close(fd)


def text(path, budget=None):
    check_deadline(budget)
    value = metadata(path, budget).decode('utf-8')
    if any(ord(c) < 32 and c != '\n' for c in value):
        raise Unknown('invalid_metadata')
    return value


def scalar(path, budget=None):
    check_deadline(budget)
    value = text(path, budget)
    if value.endswith('\n'):
        value = value[:-1]
    if not value or '\n' in value:
        raise Unknown('invalid_scalar')
    return value


def info_at(path, budget=None):
    check_deadline(budget)
    with directory(path.parent, budget) as parent:
        check_deadline(budget)
        return os.stat(path.name, dir_fd=parent, follow_symlinks=False)


def presence(path, *, folder=True, budget=None):
    # Only final-entry ENOENT is observed absence. Missing/unsafe ancestors
    # remain unknown (do not turn permission errors into an empty inventory).
    try:
        check_deadline(budget)
        with directory(path.parent, budget) as parent:
            try:
                check_deadline(budget)
                info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                return 'absent'
            valid = stat.S_ISDIR(info.st_mode) if folder else owned_regular(info)
            return 'present' if valid and not stat.S_ISLNK(info.st_mode) else 'unknown'
    except (OSError, ValueError):
        return 'unknown'


class Budget:
    def __init__(self, nodes, seconds):
        self.left = nodes
        self.deadline = time.monotonic() + seconds
        self.exhausted = False

    def time_available(self):
        if time.monotonic() >= self.deadline:
            self.exhausted = True
            return False
        return True

    def take(self):
        if self.left <= 0 or not self.time_available():
            self.exhausted = True
            return False
        self.left -= 1
        return True


def entries(path, budget, limit):
    values, complete = [], True
    with directory(path, budget) as fd:
        check_deadline(budget)
        with os.scandir(fd) as scan:
            while True:
                if not budget.time_available():
                    complete = False
                    break
                try:
                    entry = next(scan)
                except StopIteration:
                    break
                if len(values) == limit or not budget.take():
                    complete = False
                    break
                values.append(entry.name)
    return sorted(values), complete


def lease_state(record, budget=None):
    try:
        check_deadline(budget)
        with directory(record, budget) as parent:
            check_deadline(budget)
            fd = os.open('runtime.lock', READ_FLAGS, dir_fd=parent)
            try:
                check_deadline(budget)
                if not owned_regular(os.fstat(fd)):
                    return 'unknown'
                # A transient read lock conflicts with the launcher's exclusive
                # lockf lease. O_RDONLY: no creation, truncation or write access.
                try:
                    check_deadline(budget)
                    fcntl.lockf(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                except OSError as exc:
                    return 'active' if exc.errno in (errno.EACCES, errno.EAGAIN) else 'unknown'
                fcntl.lockf(fd, fcntl.LOCK_UN)
                return 'free_at_observation'
            finally:
                os.close(fd)
    except (OSError, ValueError):
        return 'unknown'


def holds(record, source, owner, budget):
    count, invalid = 0, False
    try:
        names, complete = entries(record, budget, 1000)
        for name in names:
            if not budget.time_available():
                complete = False
                break
            if not (name.startswith('resume-hold-') and name.endswith('.json')):
                continue
            count += 1
            try:
                value = json.loads(metadata(record / name, budget))
                keys = {'pid', 'start', 'source_root', 'session_id', 'request_id'}
                if (not isinstance(value, dict) or set(value) != keys or type(value['pid']) is not int
                        or value['pid'] <= 0 or any(not isinstance(value[k], str) or not value[k]
                        or any(ord(c) < 32 for c in value[k]) for k in keys - {'pid'})
                        or value['source_root'] != source or value['session_id'] != owner
                        or not re.fullmatch(r'[A-Za-z0-9._-]+', value['request_id'])):
                    raise Unknown('hold_binding')
            except (OSError, ValueError, UnicodeError, TypeError, RecursionError):
                invalid = True
        complete = budget.time_available() and complete
        return {'status': 'present_unverified' if count else ('absent' if complete else 'unknown'),
                'count_observed': count, 'metadata_unknown': invalid or not complete, 'scan_complete': complete}
    except (OSError, ValueError):
        return {'status': 'unknown', 'count_observed': None, 'metadata_unknown': True, 'scan_complete': False}


def runtime_pin(value, home, budget=None):
    if (not isinstance(value, dict) or not isinstance(value.get('revision'), str)
            or not re.fullmatch(r'[0-9a-f]{40}', value['revision'])
            or not isinstance(value.get('version'), str) or not re.fullmatch(r'[0-9A-Za-z.+-]{1,80}', value['version'])
            or not isinstance(value.get('manifest_sha256'), str)
            or not re.fullmatch(r'[0-9a-f]{64}', value['manifest_sha256'])
            or value.get('path') != str(home / value['revision'])):
        raise Unknown('runtime_pin')
    with directory(home / value['revision'], budget):
        pass
    return {key: value[key] for key in ('revision', 'version', 'path', 'manifest_sha256')} | {'verification': 'metadata_only'}


def runtimes(home, owner, source, budget=None):
    result = {'bound': None, 'selected': None, 'differs_from_selected': None, 'source_matches': None}
    if home is None:
        return result
    try:
        check_deadline(budget)
        result['selected'] = runtime_pin(json.loads(metadata(home / 'current.json', budget)), home, budget)
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        pass
    try:
        check_deadline(budget)
        value = json.loads(metadata(home / 'bindings' / f'{owner}.json', budget))
        if not isinstance(value, dict) or not isinstance(value.get('source_root'), str):
            raise Unknown('runtime_binding')
        result['bound'] = runtime_pin(value.get('runtime'), home, budget)
        result['source_matches'] = value['source_root'] == source if source is not None else None
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        pass
    if result['bound'] and result['selected']:
        result['differs_from_selected'] = any(result['bound'][key] != result['selected'][key]
            for key in ('revision', 'path', 'manifest_sha256', 'version'))
    return result


def terminal_evidence(record, lifecycle, identity, budget=None):
    if lifecycle == 'CLOSED':
        return identity == ''
    if lifecycle == 'DISCARDED':
        return (identity == '' or re.fullmatch(r'[0-9a-fA-F]{64}', identity) is not None) and presence(record / 'discarded.patch', folder=False, budget=budget) == 'present'
    if lifecycle != 'DELIVERED' or not re.fullmatch(r'[0-9a-fA-F]{64}', identity):
        return False
    try:
        if not OID.fullmatch(scalar(record / 'delivered-sha', budget)):
            return False
        check_deadline(budget)
        parts = metadata(record / 'delivered-manifest', budget).split(b'\0')
        if parts[-1] != b'' or len(parts) < 5 or (len(parts) - 1) % 4:
            return False
        for offset in range(0, len(parts) - 1, 4):
            kind, mode, oid, raw = parts[offset:offset + 4]
            path = raw.decode('utf-8')
            if (not path or path.startswith('/') or any(p in ('', '.', '..') for p in path.split('/'))
                    or any(ord(c) < 32 for c in path)):
                return False
            if kind == b'F':
                if mode not in (b'100644', b'100755', b'120000', b'160000') or not OID.fullmatch(oid.decode('ascii')):
                    return False
            elif kind != b'D' or mode != b'-' or oid != b'-':
                return False
        return True
    except (OSError, ValueError, UnicodeError):
        return False


def measure_sizes(root, budget, expected_identity):
    counts = {'nominal_regular_bytes': 0, 'allocated_regular_bytes': 0,
              'unique_inode_allocated_bytes': 0, 'shared_hardlink_allocated_bytes': 0}
    result = {'status': 'complete', 'symlinks_skipped': 0, 'files_observed': 0,
              'root_changed': False, 'physically_reclaimable_bytes': None}
    seen, complete, stack = set(), True, [root]
    while stack:
        current = stack.pop()
        try:
            with directory(current, budget) as fd:
                if current == root and (os.fstat(fd).st_dev, os.fstat(fd).st_ino) != expected_identity:
                    result['root_changed'] = True
                    raise Unknown('root_changed')
                check_deadline(budget)
                with os.scandir(fd) as scan:
                    while True:
                        if not budget.time_available():
                            complete = False
                            stack.clear()
                            break
                        try:
                            entry = next(scan)
                        except StopIteration:
                            break
                        if not budget.take():
                            complete = False
                            stack.clear()
                            break
                        info = entry.stat(follow_symlinks=False)
                        if stat.S_ISLNK(info.st_mode):
                            result['symlinks_skipped'] += 1
                        elif stat.S_ISDIR(info.st_mode):
                            if info.st_dev != os.fstat(fd).st_dev:
                                complete = False  # Do not descend into mounted trees.
                            else:
                                stack.append(current / entry.name)
                        elif stat.S_ISREG(info.st_mode):
                            result['files_observed'] += 1
                            counts['nominal_regular_bytes'] += info.st_size
                            counts['allocated_regular_bytes'] += info.st_blocks * 512
                            key = info.st_dev, info.st_ino
                            if key not in seen:
                                seen.add(key)
                                counts['unique_inode_allocated_bytes'] += info.st_blocks * 512
                                if info.st_nlink > 1:
                                    counts['shared_hardlink_allocated_bytes'] += info.st_blocks * 512
                        else:
                            complete = False
        except (OSError, ValueError):
            complete = False
    try:
        after = info_at(root, budget)
        if (after.st_dev, after.st_ino) != expected_identity or not stat.S_ISDIR(after.st_mode):
            result['root_changed'], complete = True, False
    except Deadline:
        complete = False
    except (OSError, ValueError):
        result['root_changed'], complete = True, False
    result['status'] = 'complete' if complete else ('partial' if result['files_observed'] else 'unknown')
    result.update({key: value if complete else None for key, value in counts.items()})
    result['observed'] = counts
    return result


def session_report(state, owner, home, now, retention, current_session, sizes, budget):
    record, root = state / 'sessions' / owner, state / 'worktrees' / owner
    reasons = []
    item = {'session_id': owner, 'lifecycle': 'unknown', 'age_seconds': None,
            'age_basis': 'journal.heartbeat', 'retention_seconds': retention, 'root': str(root),
            'recorded_root': None, 'source_root': None, 'source_identity': 'unknown', 'root_identity': 'unknown',
            'base_sha': None, 'lease': 'unknown', 'holds': {'status': 'unknown'}, 'runtime': None,
            'archive': {'presence': 'unknown', 'verification': 'unknown'}, 'sizes': None,
            'delete_safe': False, 'provenance_verification': 'unknown', 'retained_reasons': reasons}
    if owner.lower() == current_session.lower():
        reasons.append('current_session')
    try:
        with directory(record, budget):
            pass
    except (OSError, ValueError):
        reasons.extend(['record_unknown', 'needs_provenance_recovery', 'needs_native_verify'])
        item['eligibility'] = 'retained'
        return item
    try:
        lines = text(record / 'journal', budget).splitlines()
        if len(lines) != 3 or not lines[0].startswith('state=') or not lines[1].startswith('identity=') or not lines[2].startswith('heartbeat='):
            raise Unknown('journal')
        lifecycle, identity, heartbeat = lines[0][6:], lines[1][9:], lines[2][10:]
        if lifecycle not in STATES or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z', heartbeat):
            raise Unknown('journal')
        when = dt.datetime.strptime(heartbeat, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=dt.timezone.utc)
        item['lifecycle'], item['age_seconds'] = lifecycle, int((now - when).total_seconds())
        if lifecycle not in TERMINAL:
            reasons.append('nonterminal_state')
        elif not terminal_evidence(record, lifecycle, identity, budget):
            reasons.append('terminal_evidence_unknown')
        if item['age_seconds'] < 0:
            reasons.append('future_timestamp')
        elif item['age_seconds'] < retention:
            reasons.append('within_retention')
    except (OSError, ValueError, UnicodeError):
        reasons.append('journal_unknown')
    root_ok = False
    try:
        item['recorded_root'] = scalar(record / 'session-root', budget)
        if item['recorded_root'] != str(root):
            reasons.append('root_path_mismatch')
        else:
            with directory(root, budget) as fd:
                info = os.fstat(fd)
                expected = scalar(record / 'root-inode', budget)
                if not root_identity_matches(expected, info, root):
                    reasons.append('root_identity_mismatch')
                else:
                    item['root_identity'], root_ok = 'matches_recorded_inode', True
    except (OSError, ValueError, UnicodeError):
        reasons.append('root_unknown')
    try:
        source = scalar(record / 'source-root', budget)
        with directory(source, budget) as fd:
            if source == str(root) or (root_ok and (os.fstat(fd).st_dev, os.fstat(fd).st_ino) == (info.st_dev, info.st_ino)):
                raise Unknown('source_is_root')
        item['source_root'], item['source_identity'] = source, 'path_only'
    except (OSError, ValueError, UnicodeError):
        reasons.append('source_unknown')
    try:
        item['base_sha'] = scalar(record / 'base-sha', budget)
        if not OID.fullmatch(item['base_sha']):
            item['base_sha'] = None
            raise Unknown('base')
    except (OSError, ValueError, UnicodeError):
        reasons.append('base_unknown')
    try:
        check_deadline(budget)
        if metadata(record / 'lease-v1', budget) != b'1\n':
            raise Unknown('lease_marker')
    except (OSError, ValueError):
        reasons.append('lease_marker_unknown')
    item['lease'] = lease_state(record, budget)
    if item['lease'] == 'active':
        reasons.append('active_lease')
    elif item['lease'] == 'unknown':
        reasons.append('lease_unknown')
    item['holds'] = holds(record, item['source_root'], owner, budget)
    if item['holds']['status'] == 'present_unverified':
        reasons.append('resume_hold_present')
    if item['holds'].get('metadata_unknown'):
        reasons.append('resume_hold_unknown')
    item['runtime'] = runtimes(home, owner, item['source_root'], budget)
    if item['runtime']['bound'] is None:
        reasons.append('bound_runtime_unknown')
    if item['runtime']['selected'] is None:
        reasons.append('selected_runtime_unknown')
    if item['runtime']['source_matches'] is False:
        reasons.append('runtime_source_mismatch')
    item['archive']['presence'] = presence(state / 'archives' / owner, budget=budget)
    if sizes and root_ok:
        item['sizes'] = measure_sizes(root, budget, (info.st_dev, info.st_ino))
        if item['sizes']['root_changed']:
            item['root_identity'] = 'unknown'
            reasons.append('root_identity_mismatch')
        if item['sizes']['status'] != 'complete':
            reasons.append('scan_incomplete')
    if not budget.time_available() or not item['holds']['scan_complete']:
        if 'scan_incomplete' not in reasons:
            reasons.append('scan_incomplete')
    blockers = {'current_session', 'nonterminal_state', 'journal_unknown', 'future_timestamp', 'within_retention',
                'active_lease', 'lease_unknown', 'lease_marker_unknown', 'resume_hold_present', 'resume_hold_unknown', 'scan_incomplete'}
    # Metadata can show matching paths/inodes, but cannot prove source Git,
    # snapshot contents, archive fidelity or Native history completeness.
    reasons.extend(['needs_provenance_recovery', 'archive_verification_unknown', 'needs_native_verify'])
    item['eligibility'] = 'retained' if blockers.intersection(reasons) else 'needs_provenance_recovery'
    return item


def build_report(*, state, runtime_home=None, now=None, retention=86400, current_session='', sizes=False,
                 max_sessions=1000, max_nodes=10000, max_seconds=5):
    start = time.monotonic()
    state = Path(state)
    home = Path(runtime_home) if runtime_home is not None else None
    now = now or dt.datetime.now(dt.timezone.utc)
    budget = Budget(max_nodes, max_seconds)
    report = {'schema_version': 1, 'read_only': True, 'observed_at': now.isoformat(), 'state_root': str(state),
              'retention_seconds': retention, 'scan_status': 'complete', 'problems': [], 'sessions': [],
              'last_gc_trigger': None, 'last_gc_trigger_evidence': 'no_durable_trigger_record_read',
              'size_totals': None, 'elapsed_seconds': None}
    try:
        with directory(state, budget):
            pass
        names, complete = entries(state / 'sessions', budget, max_sessions)
        if not complete:
            report['problems'].append('session_limit' if not budget.exhausted else 'scan_budget')
        for owner in names:
            if not budget.time_available():
                report['problems'].append('time_budget')
                break
            if not UUID.fullmatch(owner):
                report['problems'].append('unrecognized_session_entry')
                continue
            report['sessions'].append(session_report(state, owner, home, now, retention, current_session, sizes, budget))
        budget.time_available()
        if budget.exhausted and 'scan_budget' not in report['problems']:
            report['problems'].append('scan_budget')
        if not complete or budget.exhausted or report['problems'] or any('scan_incomplete' in item['retained_reasons'] for item in report['sessions']):
            report['scan_status'] = 'partial'
    except Deadline:
        report['scan_status'] = 'partial'
        report['problems'].append('scan_budget')
    except (OSError, ValueError):
        report['scan_status'] = 'unknown'
        report['problems'].append('state_unknown')
    report['elapsed_seconds'] = round(time.monotonic() - start, 6)
    return report


def bounded_int(maximum):
    def parse(value):
        try:
            number = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError('expected a positive integer') from None
        if not 1 <= number <= maximum:
            raise argparse.ArgumentTypeError(f'expected 1..{maximum}')
        return number
    return parse


def main(argv=None):
    parser = argparse.ArgumentParser(description='Read-only session GC diagnostics; never authorizes deletion.')
    parser.add_argument('--json', action='store_true', required=True, help='emit one JSON report')
    parser.add_argument('--sizes', action='store_true', help='opt in to bounded file-metadata size scanning')
    parser.add_argument('--runtime-home', type=Path, help='optional managed runtime root containing current.json and bindings/')
    parser.add_argument('--max-sessions', type=bounded_int(1000), default=1000)
    parser.add_argument('--max-nodes', type=bounded_int(100000), default=10000)
    parser.add_argument('--max-seconds', type=bounded_int(30), default=5)
    args = parser.parse_args(argv)
    retention = os.environ.get('HARNESS_SESSION_RETENTION_SECONDS', '86400')
    if retention == 'archive':
        retention = '86400'
    if not re.fullmatch('[0-9]+', retention) or len(retention) > 6 or int(retention) > 604800:
        parser.error('HARNESS_SESSION_RETENTION_SECONDS must be 0..604800 or archive')
    state = Path(os.environ.get('HARNESS_SESSION_STATE_HOME', str(Path(os.environ.get('XDG_STATE_HOME', str(Path.home() / '.local/state'))) / 'harness-launcher')))
    report = build_report(state=state, runtime_home=args.runtime_home, retention=int(retention),
        current_session=os.environ.get('HARNESS_SESSION_ID', ''), sizes=args.sizes,
        max_sessions=args.max_sessions, max_nodes=args.max_nodes, max_seconds=args.max_seconds)
    print(json.dumps(report, ensure_ascii=True, separators=(',', ':')))
    return 0


if __name__ == '__main__':
    sys.exit(main())
