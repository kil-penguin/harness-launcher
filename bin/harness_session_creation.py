#!/usr/bin/env python3
"""Durable admission for isolated-session creation."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import itertools
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
from contextlib import contextmanager
import subprocess
import sys
import time
import uuid


CREATION_API = 1


class CreationError(RuntimeError):
    """A reservation or prepare operation is unsafe to continue."""


class PublicationError(CreationError):
    """A bound creation lacks a verified matching publication."""


def _identity(path: Path) -> dict[str, object]:
    meta = path.lstat()
    if stat.S_ISLNK(meta.st_mode):
        raise CreationError("creation identity is a symlink")
    return {"path": str(path.resolve()), "device": meta.st_dev, "inode": meta.st_ino}


def _engine_identity(path: Path) -> dict[str, object]:
    if not path.is_file() or path.is_symlink():
        raise CreationError("engine must be a regular executable")
    identity = _identity(path)
    identity["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return identity


def _profile_label(source: Path, environment: dict[str, str] | None = None) -> str:
    # An inherited prefix can belong to the parent session's different profile.
    value = source.name
    return value if re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', value) else 'unknown'


def _diagnostic(phase: str, source: Path, started: float, environment=None) -> None:
    environment = os.environ if environment is None else environment
    message = (f'harness-session creation: phase={phase} profile={_profile_label(source, environment)} '
               f'elapsed={max(0, time.monotonic() - started):.1f}s\n')
    descriptor = environment.get('HARNESS_CREATION_DIAGNOSTIC_FD', '')
    if descriptor.isdigit():
        try:
            os.write(int(descriptor), message.encode())
            return
        except OSError:
            pass
    print(message, file=sys.stderr, end='', flush=True)


def _same_identity(path: Path, recorded: dict[str, object]) -> bool:
    try:
        current = _identity(path)
    except (OSError, CreationError):
        return False
    if not isinstance(recorded, dict):
        return False
    return current == {key: recorded.get(key) for key in current}


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_json(path: Path, value: dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        json.dump(value, output, sort_keys=True, separators=(",", ":"))
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(_private_bytes(path))
    except (OSError, ValueError) as exc:
        raise CreationError("creation metadata is unreadable") from exc
    if not isinstance(value, dict):
        raise CreationError("creation metadata has invalid shape")
    return value


def _private_bytes(path: Path, limit: int = 65536) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        meta = os.fstat(stream.fileno())
        if (not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid() or meta.st_nlink != 1
                or meta.st_mode & 0o022 or meta.st_size > limit):
            raise CreationError('unsafe creation metadata')
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise CreationError('oversized creation metadata')
        return data


def _pin_is_valid(pin: object) -> bool:
    return isinstance(pin, dict) and set(pin) == {"version", "revision", "path", "manifest_sha256"} and all(
        isinstance(pin[key], str) and pin[key] for key in pin
    )


def _open_journal(record: Path) -> None:
    journal = record / "journal"
    if journal.exists() or journal.is_symlink():
        raise CreationError("journal already exists")
    text = "state=OPEN\nidentity=\nheartbeat=%s\n" % time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    temporary = record / f".journal.pending.{uuid.uuid4().hex}"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        output.write(text)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, journal)
    directory = os.open(record, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _normal_journal(record: Path) -> bool:
    lines = _private_bytes(record / 'journal', 4096).decode().splitlines()
    return (len(lines) == 3 and lines[0] in {'state=' + state for state in
            ('OPEN', 'ABANDONED', 'CONFLICT', 'CLOSED', 'SUBMITTED', 'INTEGRATING', 'DELIVERED', 'DISCARDED')}
            and lines[1].startswith('identity=') and not re.search(r'[\x00-\x1f\x7f]', lines[1])
            and re.fullmatch(r'heartbeat=\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ', lines[2]) is not None)


def _finish_recovery(record: Path, metadata: dict[str, object], entry: Path) -> None:
    metadata['phase'] = 'committed'
    _atomic_json(record / 'creation.json', metadata)
    (record / 'pending').unlink(missing_ok=True)
    entry.unlink()
    _sync_directory(record)
    _sync_directory(entry.parent)


def _owned_directory(path: Path, *, private: bool = False) -> None:
    pending: list[Path] = []
    current = path
    while not current.exists():
        pending.append(current)
        current = current.parent
    if current.is_symlink() or not current.is_dir():
        raise CreationError("creation directory is unsafe")
    for directory in reversed(pending):
        directory.mkdir(mode=0o700)
    stat = path.lstat()
    if not path.is_dir() or path.is_symlink() or stat.st_uid != os.getuid() or stat.st_mode & (0o077 if private else 0o022):
        raise CreationError("creation directory is unsafe")


def _anchors_intact(state: Path, metadata: dict[str, object], source: Path) -> bool:
    """Persisted metadata never selects the directories we may admit or remove."""
    uid = metadata.get("id")
    if not isinstance(uid, str) or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', uid):
        return False
    record, root = state / 'sessions' / uid, state / 'worktrees' / uid
    bucket = hashlib.sha256(str(source).encode()).hexdigest()
    if metadata.get('source_root') != str(source) or metadata.get('worktree_path') != str(root) or metadata.get('pending_bucket') != bucket:
        return False
    identities = ((state, 'state_identity'), (state / 'sessions', 'sessions_identity'),
                  (record, 'record_identity'), (source, 'source_identity'),
                  (root.parent, 'worktree_parent_identity'), (root, 'worktree_identity'),
                  (state / 'creation-pending' / bucket, 'index_parent_identity'))
    try:
        for path, key in identities:
            meta = path.lstat()
            private = path in (record, root, state / 'creation-pending' / bucket)
            if (not stat.S_ISDIR(meta.st_mode) or meta.st_uid != os.getuid()
                    or meta.st_mode & (0o077 if private else 0o022)
                    or not _same_identity(path, metadata.get(key, {}))):
                return False
        return True
    except OSError:
        return False


def _present(path: Path) -> bool:
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False


@contextmanager
def _runtime_inactive(record: Path):
    """Runtime admission uses POSIX record locks, independently of creation flock."""
    descriptor = None
    try:
        try:
            descriptor = os.open(record / 'runtime.lock', os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            yield True
            return
        meta = os.fstat(descriptor)
        if not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid() or meta.st_nlink != 1 or meta.st_mode & 0o022:
            yield False
            return
        try:
            fcntl.lockf(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _stop_engine(child: subprocess.Popen) -> None:
    """Finish bounded cleanup before a repeated terminal interrupt can unwind it."""
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
    def send(value):
        try:
            os.killpg(child.pid, value)
        except ProcessLookupError:
            pass
    try:
        send(signal.SIGTERM)
        try:
            child.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            send(signal.SIGKILL)
            try:
                child.communicate(timeout=5)
            except subprocess.TimeoutExpired as exc:
                raise CreationError('engine cleanup unconfirmed; artifacts retained') from exc
        finally:
            # The group may still have descendants after the direct child exits.
            send(signal.SIGKILL)
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


class Creation:
    def __init__(self, state: Path, source: Path, expected_pin: dict[str, str] | None = None):
        if expected_pin is not None and not _pin_is_valid(expected_pin):
            raise CreationError("expected_pin must contain the complete runtime pin")
        self.state = Path(state).absolute()
        self.source = Path(source).resolve()
        if not self.source.is_dir() or self.source.is_symlink():
            raise CreationError("source must be a real directory")
        self.expected_pin = dict(expected_pin) if expected_pin is not None else None
        self.id = str(uuid.uuid4())
        self.record = self.state / "sessions" / self.id
        self.root = self.state / "worktrees" / self.id
        self.bucket = hashlib.sha256(str(self.source).encode()).hexdigest()
        self.index = self.state / "creation-pending" / self.bucket / self.id
        self._lease: int | None = None
        self._metadata: dict[str, object] | None = None

    def __enter__(self) -> "Creation":
        try:
            return self._reserve()
        except BaseException:
            if self._lease is not None:
                os.close(self._lease)
                self._lease = None
            raise

    def _reserve(self) -> "Creation":
        _owned_directory(self.state)
        # Existing catalog/resume consumers compare the configured state prefix
        # literally (macOS /var and /private/var are both common). Identities
        # still bind the physical path/inode; retain the public path spelling.
        self.record = self.state / 'sessions' / self.id
        self.root = self.state / 'worktrees' / self.id
        self.index = self.state / 'creation-pending' / self.bucket / self.id
        sessions = self.state / "sessions"
        worktrees = self.state / "worktrees"
        _owned_directory(sessions)
        _owned_directory(worktrees)
        _owned_directory(self.state / "creation-pending", private=True)
        _owned_directory((self.state / "creation-pending") / self.bucket, private=True)
        self.record.mkdir(mode=0o700)
        self.root.mkdir(mode=0o700)
        self._lease = os.open(self.record / "creation.lock", os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        fcntl.flock(self._lease, fcntl.LOCK_EX)
        os.set_inheritable(self._lease, True)
        metadata: dict[str, object] = {
            "id": self.id,
            "source_root": str(self.source),
            "source_identity": _identity(self.source),
            "state_identity": _identity(self.state),
            "sessions_identity": _identity(sessions),
            "record_identity": _identity(self.record),
            "worktree_parent_identity": _identity(worktrees),
            "worktree_path": str(self.root),
            "worktree_identity": _identity(self.root),
            "pending_bucket": self.bucket,
            "index_parent_identity": _identity(self.index.parent),
            "creation_lease_identity": _identity(self.record / 'creation.lock'),
            "expected_pin": self.expected_pin,
            "mode": "bound" if self.expected_pin else "standalone",
            "phase": "reserved",
        }
        _atomic_json(self.record / "creation.json", metadata)
        (self.record / "pending").touch(exist_ok=False)
        _sync_directory(self.record)
        index_descriptor = os.open(self.index, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(index_descriptor, 'w') as marker:
            marker.write(self.id + '\n')
            marker.flush()
            os.fsync(marker.fileno())
        for directory in (sessions, worktrees, self.index.parent, self.index.parent.parent, self.state):
            _sync_directory(directory)
        self._metadata = metadata
        return self

    def __exit__(self, *_unused: object) -> None:
        try:
            metadata = self._metadata
            original = self._lease
            self._lease = None
            if original is None:
                return
            os.close(original)
            if metadata is not None and metadata.get("phase") in {"reserved", "preparing", "prepared"}:
                # The child inherited the original open-file description. Only
                # a fresh OFD can prove that no surviving inheritor holds it.
                self._lease = os.open(self.record / 'creation.lock', os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
                fcntl.flock(self._lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._cleanup_unpublished(metadata)
        except (OSError, CreationError):
            pass  # Uncertain cleanup preserves artifacts and the original failure.
        finally:
            if self._lease is not None:
                os.close(self._lease)
                self._lease = None

    def _cleanup_unpublished(self, metadata: dict[str, object]) -> None:
        self._require_active()
        if not _anchors_intact(self.state, metadata, self.source) or _read_json(self.record / 'creation.json') != metadata:
            return
        if _present(self.record / "journal"):
            return
        with _runtime_inactive(self.record) as inactive:
            if inactive and _anchors_intact(self.state, metadata, self.source) and not _present(self.record / 'journal'):
                shutil.rmtree(self.root)
                shutil.rmtree(self.record)
                self.index.unlink()

    def _require_active(self) -> dict[str, object]:
        if self._lease is None or self._metadata is None:
            raise CreationError("creation context is not active")
        if not _anchors_intact(self.state, self._metadata, self.source):
            raise CreationError('creation context identity changed')
        _check_creation_lease(self.record, self._metadata, self._lease)
        if _read_json(self.record / 'creation.json') != self._metadata:
            raise CreationError('creation context metadata changed')
        return self._metadata

    def _write(self, **updates: object) -> None:
        metadata = self._require_active()
        metadata.update(updates)
        _atomic_json(self.record / "creation.json", metadata)

    def prepare(self, engine: Path, env: dict[str, str], *, local: bool = False, timeout: float = 120) -> dict[str, str]:
        metadata = self._require_active()
        engine = Path(engine).resolve()
        if self.expected_pin is not None:
            expected_engine = (Path(self.expected_pin["path"]) / "bin" / "session-isolation.sh").resolve()
            if engine != expected_engine:
                raise CreationError("bound engine does not match runtime pin")
        identity = _engine_identity(engine)
        self._write(engine_identity=identity, phase="preparing")
        inherited = dict(env)
        inherited.update({
            "HARNESS_SESSION_STATE_HOME": str(self.state),
            "HARNESS_CREATION_LEASE_FD": str(self._lease),
            "HARNESS_CREATION_RECORD": str(self.record),
            "HARNESS_CREATION_SOURCE": str(self.source),
            "HARNESS_CREATION_ENGINE": str(engine),
        })
        command = [str(engine), "create", "--reserved", self.id]
        if local:
            command.append("--local")
        command.append(str(self.source))
        started = time.monotonic()
        _diagnostic('prepare', self.source, started, inherited)
        diagnostic_fd = None
        try:
            # Only our fixed phase messages use this channel. Child stderr stays
            # captured so Git errors/configured programs cannot expose payloads.
            try:
                diagnostic_fd = os.dup(sys.stderr.fileno())
            except (AttributeError, OSError, ValueError):
                pass
            descriptors = (self._lease,)
            if diagnostic_fd is not None:
                inherited['HARNESS_CREATION_DIAGNOSTIC_FD'] = str(diagnostic_fd)
                descriptors += (diagnostic_fd,)
            else:
                inherited.pop('HARNESS_CREATION_DIAGNOSTIC_FD', None)
            child = subprocess.Popen(command, env=inherited, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, pass_fds=descriptors, start_new_session=True)
            try:
                stdout, stderr = child.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                _diagnostic('reap', self.source, started, inherited)
                _stop_engine(child)
                raise CreationError("reserved engine preparation timed out")
            except KeyboardInterrupt:
                _diagnostic('reap', self.source, started, inherited)
                _stop_engine(child)
                raise
        except OSError as exc:
            raise CreationError("reserved engine preparation failed") from exc
        finally:
            if diagnostic_fd is not None:
                os.close(diagnostic_fd)
        if child.returncode:
            raise CreationError(f"reserved engine preparation failed (exit {child.returncode})")
        lines = stdout.splitlines()
        if len(lines) != 4 or any('=' not in line for line in lines):
            raise CreationError("reserved engine returned invalid creation fields")
        fields = dict(line.split("=", 1) for line in lines)
        required = {"HARNESS_SESSION_ID", "HARNESS_SOURCE_ROOT", "HARNESS_SESSION_ROOT", "HARNESS_RUN_DIR"}
        if set(fields) != required or fields["HARNESS_SESSION_ID"] != self.id:
            raise CreationError("reserved engine returned invalid creation fields")
        if (fields["HARNESS_SOURCE_ROOT"] != str(self.source) or fields["HARNESS_SESSION_ROOT"] != str(self.root)
                or fields["HARNESS_RUN_DIR"] != str(self.root)):
            raise CreationError("reserved engine returned foreign paths")
        if not _anchors_intact(self.state, metadata, self.source):
            raise CreationError("creation identity changed")
        if (self.record / "journal").exists():
            raise CreationError("reserved engine exposed a legacy journal")
        try:
            evidence = _preparation_evidence(self.record, metadata, self.source, self.root)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise CreationError('reserved engine preparation evidence is invalid') from exc
        self._write(phase="prepared", fields=fields, prepared_engine_identity=identity, prepared_evidence=evidence)
        return fields

    def publication_started(self) -> None:
        metadata = self._require_active()
        if metadata.get("phase") != "prepared":
            raise CreationError("publication requires prepared creation")
        self._write(phase="publication_started")

    def commit(self, readback: dict[str, object] | None = None) -> None:
        try:
            metadata = self._require_active()
        except (OSError, CreationError) as exc:
            raise PublicationError('creation context authority changed') from exc
        if metadata.get("phase") != "publication_started":
            raise PublicationError("commit requires publication intent")
        if not _prepared_is_intact(self.record, metadata, self.source, self.root):
            raise PublicationError("prepared creation identity changed")
        try:
            _verified_reserved(self.record, self.source, Path(str(metadata['engine_identity']['path'])), self._lease)
        except (OSError, CreationError) as exc:
            raise PublicationError('creation lease authority changed') from exc
        if self.expected_pin is None:
            if readback is not None:
                raise PublicationError("standalone commit takes no binding readback")
        elif not _matching_readback(readback, self.source, self.expected_pin):
            raise PublicationError("binding readback does not match creation intent")
        _open_journal(self.record)
        self._write(phase="committed")
        (self.record / "pending").unlink()
        if self.index.is_file() and not self.index.is_symlink():
            self.index.unlink()
        _sync_directory(self.record)
        _sync_directory(self.index.parent)


def _matching_readback(readback: object, source: Path, pin: dict[str, str]) -> bool:
    return isinstance(readback, dict) and readback.get("status") == "MATCH" and readback.get("source_root") == str(source) and readback.get("runtime") == pin


def _preparation_evidence(record: Path, metadata: dict[str, object], source: Path, root: Path) -> dict[str, object]:
    if root != record.parent.parent / 'worktrees' / record.name or not _anchors_intact(record.parent.parent, metadata, source):
        raise CreationError('creation anchors changed')
    engine = metadata.get('engine_identity')
    if not isinstance(engine, dict) or _engine_identity(Path(str(engine.get('path', '')))) != engine:
        raise CreationError('creation engine changed')
    raw = {name: _private_bytes(record / name) for name in
           ('source-root', 'session-root', 'base-sha', 'root-inode', 'lease-v1')}
    # Closing ANY descriptor for this inode drops this process's POSIX locks.
    # Evidence inspection must preserve a caller's existing native lease.
    runtime = (record / 'runtime.lock').lstat()
    if (not stat.S_ISREG(runtime.st_mode) or runtime.st_uid != os.getuid() or runtime.st_nlink != 1
            or runtime.st_mode & 0o022 or runtime.st_size != 0):
        raise CreationError('creation runtime lease changed')
    raw['runtime.lock'] = b''
    if raw['source-root'].decode().strip() != str(source) or raw['session-root'].decode().strip() != str(root):
        raise CreationError('creation paths changed')
    base = raw['base-sha'].decode().strip()
    current = root.stat()
    if (not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', base)
            or raw['root-inode'].decode().strip() != f'{current.st_dev} {current.st_ino}'
            or raw['lease-v1'] != b'1\n' or raw['runtime.lock'] != b''):
        raise CreationError('creation lease or base evidence changed')
    if not (root / '.git').is_dir() or (root / '.git').is_symlink() or _git(root, 'rev-parse', '--verify', 'HEAD^{commit}') != base:
        raise CreationError('creation workspace base changed')
    return {'base_sha': base, 'files': {name: {'identity': _identity(record / name),
            'sha256': hashlib.sha256(data).hexdigest()} for name, data in raw.items()}, 'engine': engine}


def _prepared_is_intact(record: Path, metadata: dict[str, object], source: Path, root: Path) -> bool:
    try:
        return _preparation_evidence(record, metadata, source, root) == metadata.get('prepared_evidence')
    except (OSError, ValueError, TypeError, CreationError, subprocess.SubprocessError):
        return False


def _cleanup_abandoned(record: Path, root: Path, entry: Path, metadata: dict[str, object], source: Path) -> bool:
    with _runtime_inactive(record) as inactive:
        if not inactive or not _anchors_intact(record.parent.parent, metadata, source) or _present(record / 'journal'):
            return False
        shutil.rmtree(root)
        shutil.rmtree(record)
        entry.unlink()
        return True


def _check_creation_lease(record: Path, metadata: dict[str, object], lease_fd: int) -> None:
    lock = record / "creation.lock"
    meta = os.fstat(lease_fd)
    if (not _same_identity(lock, metadata.get('creation_lease_identity', {}))
            or meta.st_ino != lock.lstat().st_ino or meta.st_dev != lock.lstat().st_dev
            or not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid() or meta.st_nlink != 1 or meta.st_mode & 0o022):
        raise CreationError("creation lease descriptor does not match record")
    probe = os.open(lock, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            raise CreationError("creation lease descriptor is not locked")
        try:
            fcntl.flock(lease_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CreationError("creation lease belongs to another process") from exc
    finally:
        os.close(probe)


def _verified_reserved(record: Path, source: Path, engine: Path, lease_fd: int) -> dict[str, object]:
    metadata = _read_json(record / "creation.json")
    if metadata.get("id") != record.name or not _anchors_intact(record.parent.parent, metadata, source.resolve()):
        raise CreationError("reserved creation source does not match")
    _check_creation_lease(record, metadata, lease_fd)
    if metadata.get("engine_identity") != _engine_identity(engine):
        raise CreationError("reserved creation engine changed")
    if not _same_identity(record, metadata.get("record_identity", {})):
        raise CreationError("reserved creation record changed")
    return metadata


def reconcile(state: Path, source: Path, binding_lookup=None) -> dict[str, object]:
    state = Path(state).absolute()
    source = Path(source).resolve()
    outcomes: list[dict[str, str]] = []
    sessions = state / "sessions"
    bucket = state / "creation-pending" / hashlib.sha256(str(source).encode()).hexdigest()
    if not sessions.is_dir() or sessions.is_symlink() or not bucket.is_dir() or bucket.is_symlink():
        return {"scanned": 0, "outcomes": outcomes}
    with os.scandir(bucket) as candidates:
        entries = list(itertools.islice(candidates, 129))
    limited = len(entries) > 128
    for raw_entry in entries[:128]:
        entry = Path(raw_entry.path)
        invalid = {'id': entry.name, 'outcome': 'invalid', 'phase': 'unknown'}
        try:
            if not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', entry.name) or _private_bytes(entry, 64) != (entry.name + '\n').encode():
                outcomes.append(invalid)
                continue
        except (OSError, CreationError):
            outcomes.append(invalid)
            continue
        record = sessions / entry.name
        pending = record / "pending"
        if not record.is_dir() or record.is_symlink() or pending.is_symlink():
            continue
        try:
            metadata = _read_json(record / "creation.json")
        except CreationError:
            outcomes.append(invalid)
            continue
        if metadata.get("source_root") != str(source):
            outcomes.append(invalid)
            continue
        if metadata.get("pending_bucket") != hashlib.sha256(str(source).encode()).hexdigest():
            outcomes.append({"id": record.name, "outcome": "invalid", "phase": "unknown"})
            continue
        lease = record / "creation.lock"
        if not lease.is_file() or lease.is_symlink():
            outcomes.append({"id": record.name, "outcome": "invalid", "phase": "unknown"})
            continue
        try:
            descriptor = os.open(lease, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError:
            outcomes.append(invalid)
            continue
        try:
            locked_meta = os.fstat(descriptor)
            expected_lease = metadata.get('creation_lease_identity', {})
            if (not _same_identity(lease, expected_lease) or not isinstance(expected_lease, dict)
                    or locked_meta.st_ino != expected_lease.get('inode') or locked_meta.st_dev != expected_lease.get('device')
                    or not stat.S_ISREG(locked_meta.st_mode) or locked_meta.st_uid != os.getuid()
                    or locked_meta.st_nlink != 1 or locked_meta.st_mode & 0o022):
                outcomes.append({'id': record.name, 'outcome': 'invalid', 'phase': 'unknown'})
                continue
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                outcomes.append({"id": record.name, "outcome": "live", "phase": "reserved"})
                continue
            if metadata.get("id") != record.name or not _anchors_intact(state, metadata, source):
                outcomes.append({"id": record.name, "outcome": "invalid", "phase": "unknown"})
                continue
            root = state / 'worktrees' / record.name
            intact = _prepared_is_intact(record, metadata, source, root)
            readback = {'status': 'UNKNOWN'}
            if metadata.get("mode") == "bound":
                try:
                    readback = binding_lookup(metadata) if binding_lookup else {"status": "UNKNOWN"}
                except Exception:
                    readback = {"status": "UNKNOWN"}
            if _present(record / 'journal'):
                proof = metadata.get('phase') == 'committed' or (metadata.get('phase') == 'publication_started' and intact)
                authority = ((metadata.get('mode') == 'bound' and _matching_readback(readback, source, metadata.get('expected_pin')))
                             or metadata.get('mode') == 'standalone')
                if proof and authority and _normal_journal(record):
                    _finish_recovery(record, metadata, entry)
                    outcomes.append({'id': record.name, 'outcome': 'committed', 'phase': 'committed'})
                else:
                    outcomes.append({'id': record.name, 'outcome': 'preserved', 'phase': str(metadata.get('phase'))})
                continue
            if not pending.is_file():
                outcomes.append(invalid)
                continue
            if metadata.get('mode') == 'bound':
                if (_matching_readback(readback, source, metadata.get("expected_pin"))
                        and metadata.get("phase") == "publication_started" and intact):
                    _open_journal(record)
                    _finish_recovery(record, metadata, entry)
                    outcomes.append({"id": record.name, "outcome": "committed", "phase": "committed"})
                elif isinstance(readback, dict) and readback.get("status") == "ABSENT" and metadata.get("phase") in {'prepared', 'publication_started'} and intact:
                    outcome = "cleaned" if _cleanup_abandoned(record, root, entry, metadata, source) else "preserved"
                    outcomes.append({"id": record.name, "outcome": outcome, "phase": str(metadata['phase'])})
                else:
                    outcomes.append({"id": record.name, "outcome": "preserved", "phase": str(metadata.get("phase"))})
            elif metadata.get("phase") == "publication_started" and intact:
                _open_journal(record)
                _finish_recovery(record, metadata, entry)
                outcomes.append({"id": record.name, "outcome": "committed", "phase": "committed"})
            else:
                outcomes.append({"id": record.name, "outcome": "preserved", "phase": str(metadata.get("phase"))})
        except (OSError, CreationError, ValueError, TypeError, KeyError):
            outcomes.append(invalid)
        finally:
            os.close(descriptor)
    return {"scanned": min(len(entries), 128), "outcomes": outcomes, "limited": limited}


def _legacy_create(arguments: argparse.Namespace) -> int:
    with Creation(Path(arguments.state), Path(arguments.source)) as creation:
        fields = creation.prepare(Path(arguments.engine), dict(os.environ), local=arguments.local)
        creation.publication_started()
        creation.commit()
    for key in ("HARNESS_SESSION_ID", "HARNESS_SOURCE_ROOT", "HARNESS_SESSION_ROOT", "HARNESS_RUN_DIR"):
        print(f"{key}={fields[key]}")
    return 0


def _verify_reserved(arguments: argparse.Namespace) -> int:
    _verified_reserved(Path(arguments.record), Path(arguments.source), Path(arguments.engine), int(arguments.lease_fd))
    return 0


def _git(source: Path, *arguments: str) -> str:
    environment = dict(os.environ)
    for name in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR', 'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_NAMESPACE'):
        environment.pop(name, None)
    return subprocess.check_output(['git', '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=/dev/null',
        '-C', str(source), *arguments], env=environment, timeout=10, text=True, stderr=subprocess.DEVNULL).strip()


def _source_base(arguments: argparse.Namespace) -> int:
    source = Path(arguments.source).resolve()
    common = Path(_git(source, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    lock = common / "harness-session-fetch.lock"
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    started = time.monotonic()
    announced = False
    try:
        meta = os.fstat(descriptor)
        if not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid() or meta.st_nlink != 1 or meta.st_mode & 0o022:
            raise CreationError('unsafe source lock')
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not announced:
                    _diagnostic('source_wait', source, started)
                    announced = True
                if time.monotonic() - started >= 10:
                    _diagnostic('source_wait_timeout', source, started)
                    raise CreationError("source fetch lock timed out")
                time.sleep(0.1)
        _diagnostic('source_base', source, started)
        try:
            has_origin = subprocess.run(["git", "-C", str(source), "remote", "get-url", "origin"],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
            if has_origin and not arguments.local:
                _diagnostic('source_fetch', source, started)
                try:
                    subprocess.run(["git", "-C", str(source), "fetch", "-q", "origin", "main:refs/remotes/origin/main"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
                except subprocess.CalledProcessError as exc:
                    raise CreationError('source fetch failed') from exc
            if arguments.local and has_origin:
                base = _git(source, "rev-parse", "--verify", "refs/remotes/origin/main")
            elif arguments.local:
                base = _git(source, "rev-parse", "--verify", "refs/heads/main")
            else:
                try:
                    base = _git(source, "rev-parse", "--verify", "origin/main")
                except subprocess.CalledProcessError:
                    base = _git(source, "rev-parse", "HEAD")
            print(_git(source, "rev-parse", f"{base}^{{commit}}"))
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    command = parser.add_subparsers(dest="command", required=True)
    legacy = command.add_parser("legacy-create")
    legacy.add_argument("--engine", required=True)
    legacy.add_argument("--state", required=True)
    legacy.add_argument("--local", action="store_true")
    legacy.add_argument("source")
    verify = command.add_parser("verify-reserved")
    verify.add_argument("--record", required=True)
    verify.add_argument("--source", required=True)
    verify.add_argument("--engine", required=True)
    verify.add_argument("--lease-fd", required=True)
    source_base = command.add_parser("source-base")
    source_base.add_argument("--source", required=True)
    source_base.add_argument("--local", action="store_true")
    arguments = parser.parse_args()
    try:
        if arguments.command == "legacy-create":
            return _legacy_create(arguments)
        if arguments.command == "verify-reserved":
            return _verify_reserved(arguments)
        return _source_base(arguments)
    except CreationError as exc:
        print(f"harness-session creation: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    except (OSError, subprocess.SubprocessError):
        print('harness-session creation: phase=failed; artifacts retained when outcome is uncertain', file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
