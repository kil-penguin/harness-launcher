#!/usr/bin/env python3
"""Durable, opt-in session delivery. No service is installed by ordinary commands.

The coordinator keeps the old pool locks as compatibility gates. Its children
inherit those gates and their execution lease, so killing the coordinator never
lets an old broker race a still-running child. Metadata transactions never wait
for Git, verification, or an execution lease.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import plistlib
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import time
from urllib.parse import unquote, urlsplit
import uuid

sys.dont_write_bytecode = True
ACTIVE = ('queued', 'running', 'needs_reconcile')
POOL_OWNER = '00000000-0000-0000-0000-000000000000'
UUID = re.compile(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}')
INPUTS = ('base-sha', 'source-root', 'remote-url', 'submission.patch', 'manifest')
LIMIT = 64 * 1024 * 1024


class QueueError(ValueError):
    pass


def absolute(value):
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts or any(ord(c) < 32 for c in str(path)):
        raise QueueError('an absolute, normalized path is required')
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise QueueError('redirected integration path')
    return path


def private_directory(path):
    path = absolute(path)
    missing = []; current = path
    while not current.exists(): missing.append(current); current = current.parent
    for directory in reversed(missing):
        try: directory.mkdir(mode=0o700)
        except FileExistsError: pass
        sync_directory(directory.parent)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise QueueError('integration directory must be private and owned')
    return path


def read(path, limit=LIMIT):
    absolute(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_mode & 0o022 or info.st_size > limit):
            raise QueueError('unsafe or oversized integration file')
        value = stream.read(limit + 1)
        if len(value) > limit:
            raise QueueError('integration file exceeds limit')
        return value


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try: os.fsync(fd)
    finally: os.close(fd)


def publish(path, data, mode=0o600):
    absolute(path)
    temporary = path.with_name('.' + str(uuid.uuid4()))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        if path.is_symlink(): raise QueueError('redirected integration output')
        os.replace(temporary, path); sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode() + b'\n'


def load(path):
    return json.loads(read(path))


def digest(data):
    return hashlib.sha256(data).hexdigest()


def journal(record):
    rows = read(record/'journal', 4096).decode().splitlines()
    if len(rows) != 3 or not rows[0].startswith('state=') or not rows[1].startswith('identity='):
        raise QueueError('invalid session journal')
    return dict(row.split('=', 1) for row in rows)


def lock(path, *, shared=False):
    absolute(path)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o022:
        os.close(fd); raise QueueError('unsafe integration lease')
    try: fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd); return None
    return fd


@contextmanager
def acquired(path, *, shared=False):
    fd = lock(path, shared=shared)
    if fd is None: raise QueueError('integration operation is already owned')
    try: yield fd
    finally: os.close(fd)


def git(source, *args):
    result = subprocess.run(['git', '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=/dev/null',
                             '-C', str(source), *args], capture_output=True, text=True, timeout=30)
    if result.returncode: raise QueueError('cannot verify the canonical source repository')
    return result.stdout.strip()


def destination(remote, source):
    # Identity only; the broker still compares and uses the exact recorded URL.
    if remote.startswith('file://'):
        parsed = urlsplit(remote)
        if parsed.hostname not in (None, '', 'localhost'): raise QueueError('nonlocal file origin')
        key = 'file:' + str(Path(unquote(parsed.path)).resolve())
    elif remote.startswith(('/', './', '../')):
        key = 'file:' + str((source/remote).resolve())
    else:
        parsed = urlsplit(remote)
        if parsed.scheme:
            host = (parsed.hostname or '').lower()
            path = parsed.path.rstrip('/').removesuffix('.git')
            port = parsed.port
            if parsed.scheme == 'ssh': host, port = ssh_host(host, port)
            key = host + (':' + str(port) if port and port not in (22, 443) else '') + '/' + path.lstrip('/')
        else:
            match = re.fullmatch(r'(?:[^/@:]+@)?([^/:]+):(.+)', remote)
            if not match: key = 'file:' + str((source/remote).resolve())
            else:
                host, port = ssh_host(match[1].lower())
                key = host + (':' + str(port) if port != 22 else '') + '/' + match[2].rstrip('/').removesuffix('.git')
        if key.startswith('github.com/'): key = key.casefold()
    return digest((key + '\nrefs/heads/main').encode())


def ssh_host(host, port=None):
    # Read trusted OpenSSH configuration without connecting or canonical DNS.
    config = Path.home()/'.ssh/config'
    if config.exists(): read(config, 1024 * 1024)
    command = ['ssh', '-G', '-o', 'CanonicalizeHostname=no', '-F', str(config) if config.exists() else '/dev/null']
    if port is not None: command.extend(('-p', str(port)))
    command.append(host)
    result = subprocess.run(command, capture_output=True, text=True, timeout=5)
    if result.returncode: raise QueueError('cannot resolve the trusted SSH destination alias')
    fields = dict(line.split(None, 1) for line in result.stdout.splitlines() if ' ' in line)
    return fields['hostname'].lower(), int(fields['port'])


class Queue:
    def __init__(self, home):
        self.home = private_directory(home)
        for name in ('jobs', 'runtimes', 'leases', 'logs'):
            private_directory(self.home/name)
        database = self.home/'queue.sqlite3'
        if database.exists():
            fd = os.open(database, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o077:
                    raise QueueError('unsafe integration ledger')
            finally: os.close(fd)
        self.db = sqlite3.connect(database, timeout=5)
        database.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('PRAGMA busy_timeout=5000')
        version = self.db.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0, 1): raise QueueError('unsupported integration ledger version')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS pools(state_root TEXT PRIMARY KEY);
          CREATE TABLE IF NOT EXISTS jobs(
            sequence INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT UNIQUE NOT NULL,
            state_root TEXT NOT NULL, owner TEXT NOT NULL, operation TEXT NOT NULL,
            identity TEXT NOT NULL, destination TEXT NOT NULL, pin TEXT NOT NULL, input_digest TEXT NOT NULL,
            arguments TEXT NOT NULL DEFAULT '{}',
            state TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0,
            created REAL NOT NULL, started REAL, finished REAL, returncode INTEGER,
            reason TEXT, phase TEXT NOT NULL DEFAULT 'queued', phase_started REAL,
            worker_pid INTEGER, coordinator_pid INTEGER, publish_intent INTEGER NOT NULL DEFAULT 0,
            UNIQUE(state_root,owner,operation,identity));
          PRAGMA user_version=1;
        ''')
        self.db.commit()

    def close(self):
        self.db.close()

    def row(self, job):
        if not UUID.fullmatch(job): raise QueueError('invalid job identifier')
        result = self.db.execute('SELECT * FROM jobs WHERE job_id=?', (job,)).fetchone()
        if result is None: raise QueueError('unknown integration job')
        return dict(result)

    def status(self, job):
        row = self.row(job)
        result = {key: row[key] for key in ('sequence', 'job_id', 'owner', 'operation', 'destination', 'state', 'attempt',
                                         'created', 'started', 'finished', 'returncode', 'reason', 'phase', 'phase_started',
                                         'worker_pid', 'coordinator_pid')}
        result['queue_wait_seconds'] = None if row['started'] is None else max(0, row['started']-row['created'])
        result['phase_elapsed_seconds'] = None if row['phase_started'] is None else max(0, (row['finished'] or time.time())-row['phase_started'])
        return result

    def pin(self, engine):
        engine = absolute(engine)
        files = {}
        for path in sorted(engine.parent.iterdir()):
            if path.name.startswith('.') or path.is_dir(): continue
            files[path.name] = {'sha256': digest(read(path)), 'executable': bool(path.stat().st_mode & 0o111)}
        if 'session-isolation.sh' not in files or 'harness_integration_queue.py' not in files:
            raise QueueError('integration runtime is incomplete')
        pin = digest(canonical_json(files))
        target = self.home/'runtimes'/pin
        if not target.exists():
            stage = private_directory(self.home/'runtimes'/('.'+str(uuid.uuid4())))
            try:
                private_directory(stage/'bin')
                for name, meta in files.items():
                    data = read(engine.parent/name)
                    if digest(data) != meta['sha256']: raise QueueError('runtime changed during pinning')
                    publish(stage/'bin'/name, data, 0o500 if meta['executable'] else 0o400)
                publish(stage/'manifest.json', canonical_json(files), 0o400)
                try: os.rename(stage, target)
                except FileExistsError: shutil.rmtree(stage)
                sync_directory(target.parent)
            finally:
                if stage.exists(): shutil.rmtree(stage)
        self.verify_runtime(pin)
        return pin

    def verify_runtime(self, pin):
        if not re.fullmatch('[0-9a-f]{64}', pin): raise QueueError('invalid runtime pin')
        root = self.home/'runtimes'/pin
        files = load(root/'manifest.json')
        if digest(canonical_json(files)) != pin: raise QueueError('runtime manifest changed')
        for name, meta in files.items():
            if Path(name).name != name or digest(read(root/'bin'/name)) != meta['sha256']:
                raise QueueError('pinned integration runtime changed')
        return root/'bin/session-isolation.sh'

    def enable(self, state, engine, jobs, test_jobs):
        if not 1 <= jobs <= 4 or not 1 <= test_jobs <= 16 or jobs * test_jobs > 16:
            raise QueueError('worker/test budget must be positive and at most 16 in total')
        state = absolute(state); state.mkdir(mode=0o700, parents=True, exist_ok=True)
        marker = state/'integration-queue.json'
        config = {'schema': 1, 'enabled': True, 'jobs': jobs, 'test_jobs': test_jobs, 'pin': self.pin(engine)}
        with acquired(self.home/'configuration.lock'), acquired(self.home/'coordinator.lock'):
            if ((self.home/'config.json').exists() and load(self.home/'config.json').get('enabled') is False
                    and self.db.execute('SELECT 1 FROM pools LIMIT 1').fetchone()):
                raise QueueError('complete interrupted queue disable before enrolling pools again')
            if (self.home/'config.json').exists() and load(self.home/'config.json') != config:
                publish(self.home/'config.json', canonical_json(config))
            elif not (self.home/'config.json').exists():
                publish(self.home/'config.json', canonical_json(config))
            expected = {'schema': 1, 'home': str(self.home)}
            if marker.exists():
                if load(marker) != expected: raise QueueError('pool is enrolled in another queue')
            else:
                # A busy old writer must finish first; never kill it or edit its state.
                with acquired(state/'integration.lock'):
                    publish(marker, canonical_json(expected))
            self.db.execute('INSERT OR IGNORE INTO pools VALUES(?)', (str(state),)); self.db.commit()

    def disable(self):
        # Stop service claims first; refuse rollback with any accepted uncertainty.
        with acquired(self.home/'configuration.lock'), acquired(self.home/'coordinator.lock'):
            active = self.db.execute("SELECT 1 FROM jobs WHERE state IN ('queued','running','needs_reconcile') LIMIT 1").fetchone()
            if active: raise QueueError('accepted or indeterminate jobs must finish before disabling the queue')
            config = load(self.home/'config.json'); interrupted = config.get('enabled') is False
            gates = []
            try:
                pools = [Path(row[0]) for row in self.db.execute('SELECT state_root FROM pools ORDER BY state_root')]
                for state in pools:
                    marker = state/'integration-queue.json'
                    if marker.exists() or marker.is_symlink(): self.enrolled(state)
                    elif not interrupted: raise QueueError('pool marker is missing; inspect before rollback')
                    fd = lock(state/'integration.lock')
                    if fd is None: raise QueueError('a legacy writer still owns a pool')
                    gates.append(fd)
                config['enabled'] = False; publish(self.home/'config.json', canonical_json(config))
                for state in pools:
                    (state/'integration-queue.json').unlink(missing_ok=True); sync_directory(state)
                self.db.execute('DELETE FROM pools'); self.db.commit()
            finally:
                for fd in gates: os.close(fd)

    def enrolled(self, state):
        marker = load(absolute(state)/'integration-queue.json')
        if marker != {'schema': 1, 'home': str(self.home)}:
            raise QueueError('pool is not enrolled in this integration queue')

    @contextmanager
    def admission(self, state):
        # Snapshotting does not hold metadata locks. Publication and rollback do.
        with acquired(self.home/'configuration.lock'):
            if not load(self.home/'config.json').get('enabled'):
                raise QueueError('integration queue was disabled before acceptance')
            self.enrolled(state)
            if not self.db.execute('SELECT 1 FROM pools WHERE state_root=?', (str(state),)).fetchone():
                raise QueueError('pool registration changed before acceptance')
            yield
            self.db.commit()

    def enqueue(self, state, owner, engine, operation='integrate'):
        state = absolute(state); self.enrolled(state)
        if not UUID.fullmatch(owner): raise QueueError('invalid session identifier')
        record = state/'sessions'/owner
        # A retrying caller only reads the durable receipt. It need not take
        # the execution lease held by the worker that already owns this input.
        identity = journal(record)['identity']
        old = self.db.execute('SELECT job_id FROM jobs WHERE state_root=? AND owner=? AND operation=? AND identity=?',
                              (str(state), owner, operation, identity)).fetchone()
        if old: return old[0]
        with acquired(record/'broker.lock'):
            current = journal(record)
            if operation == 'integrate' and current['state'] not in ('SUBMITTED', 'INTEGRATING', 'DELIVERED'):
                raise QueueError('only a submitted session may enter the integration queue')
            identity = current['identity']
            old = self.db.execute('SELECT job_id FROM jobs WHERE state_root=? AND owner=? AND operation=? AND identity=?',
                                  (str(state), owner, operation, identity)).fetchone()
            if old: return old[0]
            if current['state'] != 'SUBMITTED' and operation == 'integrate':
                raise QueueError('existing integration needs explicit recovery before adoption')
            files = {name: read(record/name) for name in INPUTS}
            check = ''.join(digest(files[name]) + '  ' + str(record/name) + '\n' for name in INPUTS)
            if operation == 'integrate' and digest(check.encode()) != identity:
                raise QueueError('submission identity changed')
            for name in ('commit-message', 'headless'):
                if (record/name).exists(): files[name] = read(record/name)
            source = absolute(files['source-root'].decode().strip())
            remote = files['remote-url'].decode().strip()
            if git(source, 'remote', 'get-url', 'origin') != remote:
                raise QueueError('canonical source origin changed')
            job = str(uuid.uuid4()); root = private_directory(self.home/'jobs'/job)
            private_directory(root/'input')
            hashes = {}
            for name, data in files.items():
                publish(root/'input'/name, data, 0o400); hashes[name] = digest(data)
            publish(root/'inputs.json', canonical_json(hashes), 0o400)
            pin = self.pin(engine)
            lane = destination(remote, source)
            with self.admission(state):
                self.db.execute('INSERT INTO jobs(job_id,state_root,owner,operation,identity,destination,pin,input_digest,state,created) VALUES(?,?,?,?,?,?,?,?,?,?)',
                                (job, str(state), owner, operation, identity, lane, pin,
                                 digest(canonical_json({'files': hashes, 'arguments': {}})), 'queued', time.time()))
            return job

    def control(self, state, owner, engine, operation, arguments):
        state = absolute(state); self.enrolled(state)
        if operation not in ('recover', 'discard', 'gc'): raise QueueError('unsupported queue operation')
        if not UUID.fullmatch(owner): raise QueueError('invalid session identifier')
        lease_path = state/'broker-maintenance.lock' if operation == 'gc' else state/'sessions'/owner/'broker.lock'
        with acquired(lease_path):
            if operation != 'gc':
                journal(state/'sessions'/owner)
                active = self.db.execute("SELECT 1 FROM jobs WHERE state_root=? AND owner=? AND state IN ('queued','running','needs_reconcile')",
                                         (str(state), owner)).fetchone()
                if active: raise QueueError('an accepted job owns this session; inspect that job first')
            job = str(uuid.uuid4()); root = private_directory(self.home/'jobs'/job)
            private_directory(root/'input'); publish(root/'inputs.json', canonical_json({}), 0o400)
            pin = self.pin(engine)
            with self.admission(state):
                self.db.execute('INSERT INTO jobs(job_id,state_root,owner,operation,identity,destination,pin,input_digest,arguments,state,created) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                                (job, str(state), owner, operation, job, 'pool:'+digest(str(state).encode()) if operation == 'gc' else 'session:'+digest((str(state)+owner).encode()),
                                 pin, digest(canonical_json({'files': {}, 'arguments': arguments})),
                                 canonical_json(arguments).decode(), 'queued', time.time()))
            return job

    def verify_inputs(self, row):
        root = self.home/'jobs'/row['job_id']
        hashes = load(root/'inputs.json')
        if digest(canonical_json({'files': hashes, 'arguments': json.loads(row['arguments'])})) != row['input_digest']:
            raise QueueError('accepted input manifest changed')
        if row['operation'] == 'integrate' and not all(name in hashes for name in INPUTS):
            raise QueueError('incomplete accepted input')
        for name, expected in hashes.items():
            if Path(name).name != name or digest(read(root/'input'/name)) != expected:
                raise QueueError('accepted integration input changed')
        if row['operation'] != 'integrate': return root/'input'
        record = Path(row['state_root'])/'sessions'/row['owner']
        if journal(record)['identity'] != row['identity']: raise QueueError('session identity changed after acceptance')
        for name in ('source-root', 'base-sha', 'remote-url'):
            if read(record/name) != read(root/'input'/name): raise QueueError('session binding changed after acceptance')
        return root/'input'

    def finish(self, job, rc, reason=None):
        row = self.row(job)
        if row['operation'] == 'integrate':
            record = Path(row['state_root'])/'sessions'/row['owner']
            state = journal(record)['state']
            result = 'delivered' if state == 'DELIVERED' else 'failed'
            if state == 'INTEGRATING': result = 'needs_reconcile'
        else: result = 'completed' if rc == 0 else 'failed'
        self.db.execute('UPDATE jobs SET state=?,phase=?,returncode=?,reason=?,finished=? WHERE job_id=?',
                        (result, result, rc, reason, time.time(), job)); self.db.commit()

    def service_plist(self):
        config = load(self.home/'config.json')
        if not config.get('enabled'): raise QueueError('integration queue is disabled')
        engine = self.verify_runtime(config['pin'])
        return plistlib.dumps({'Label': 'local.harness-launcher.integration',
            'ProgramArguments': [str(Path(sys.executable).resolve()), '-I', str(engine.with_name('harness_integration_queue.py')),
                                 'serve', '--home', str(self.home)],
            'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 10,
            'EnvironmentVariables': {'HOME': str(Path.home()), 'PATH': os.environ.get('PATH', '/usr/bin:/bin')},
            'StandardOutPath': str(self.home/'logs/service.log'),
            'StandardErrorPath': str(self.home/'logs/service.log')})

    def install_service(self):
        # Installation is explicit and does not bootstrap/kill a running job.
        with acquired(self.home/'coordinator.lock'):
            data = self.service_plist()
            directory = absolute(Path.home()/'Library/LaunchAgents')
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = directory.stat()
            if info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise QueueError('unsafe LaunchAgents directory')
            target = directory/'local.harness-launcher.integration.plist'
            if target.exists():
                prior = plistlib.loads(read(target))
                expected = ['serve', '--home', str(self.home)]
                if prior.get('Label') != 'local.harness-launcher.integration' or prior.get('ProgramArguments', [])[-3:] != expected:
                    raise QueueError('refusing to replace another service registration')
            publish(target, data)
            return target


def home_for(state, explicit=None):
    if explicit: return absolute(explicit)
    marker = absolute(state)/'integration-queue.json'
    if marker.exists(): return absolute(load(marker)['home'])
    raise QueueError('integration queue is not enabled for this pool; run queue enable explicitly')


def authorize(queue, operation, owner):
    job = os.environ.get('HARNESS_INTEGRATION_JOB', '')
    row = queue.row(job)
    if row['state'] != 'running' or row['operation'] != operation or row['owner'] != owner:
        raise QueueError('integration claim is not current')
    if str(row['attempt']) != os.environ.get('HARNESS_INTEGRATION_ATTEMPT'):
        raise QueueError('integration attempt is obsolete')
    if operation == 'gc':
        arguments = json.loads(row['arguments'])
        for name, key in (('HARNESS_SESSION_RETENTION_SECONDS', 'retention'), ('HARNESS_GC_OWNER_FILTER', 'owner_filter'),
                          ('HARNESS_GC_SOURCE_FILTER', 'source_filter')):
            if os.environ.get(name, '') != str(arguments.get(key, '')):
                raise QueueError('maintenance arguments do not match the accepted operation')
    state = absolute(row['state_root'])
    if os.environ.get('HARNESS_SESSION_STATE_HOME') != str(state):
        raise QueueError('integration claim belongs to another state pool')
    engine = queue.verify_runtime(row['pin'])
    if Path(__file__).resolve() != engine.with_name('harness_integration_queue.py'):
        raise QueueError('integration claim belongs to another runtime')
    record_lease = state/'broker-maintenance.lock' if operation == 'gc' else state/'sessions'/row['owner']/'broker.lock'
    pools = [Path(entry[0]) for entry in queue.db.execute('SELECT state_root FROM pools ORDER BY state_root')]
    expected = (queue.home/'coordinator.lock', record_lease, state/'integration.lock', state/'queue-operations.lock',
                *(pool/'integration.lock' for pool in pools if pool != state))
    fds = os.environ.get('HARNESS_INTEGRATION_LEASES', '').split(',')
    if len(fds) != len(expected): raise QueueError('integration execution leases are missing')
    for number, path in zip(fds, expected):
        if not number.isdigit(): raise QueueError('invalid execution lease')
        info = os.fstat(int(number)); actual = path.stat()
        if (info.st_dev, info.st_ino) != (actual.st_dev, actual.st_ino):
            raise QueueError('integration execution lease was replaced')
        mode = fcntl.LOCK_SH if path == state/'queue-operations.lock' and operation != 'gc' else fcntl.LOCK_EX
        try: fcntl.flock(int(number), mode | fcntl.LOCK_NB)
        except BlockingIOError as exc: raise QueueError('integration descriptor does not own its execution lease') from exc
    return queue.verify_inputs(row)


def group_alive(group, exclude=()):
    # Zombies own neither execution slots nor descriptors. The system reaper may
    # observe an orphan after its work/leases already ended. On macOS killpg(0)
    # can return EPERM for such an empty/zombie group; inspect live members.
    child = subprocess.Popen(['ps', '-axo', 'pid=,pgid=,stat='], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try: stdout, _ = child.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        child.kill(); child.communicate(); return True
    if child.returncode: return True
    excluded = {str(pid) for pid in (*exclude, child.pid)}
    return any(len(parts := line.split()) == 3 and parts[0] not in excluded
               and parts[1] == str(group) and not parts[2].startswith('Z') for line in stdout.splitlines())


def guardian(queue):
    row = queue.row(os.environ.get('HARNESS_INTEGRATION_JOB', ''))
    authorize(queue, row['operation'], row['owner'])
    group = os.getpgrp()
    deadline = time.monotonic()+5
    while row['worker_pid'] != group and time.monotonic() < deadline:
        time.sleep(.01); row = queue.row(row['job_id'])
    if row['worker_pid'] != group: raise QueueError('guardian is outside its owned execution group')
    # Keep trusted lease custody outside candidate tests. In particular, a
    # verifier must not inherit a descriptor on which it could call LOCK_UN.
    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP): signal.signal(number, lambda *_: None)
    publish(queue.home/'jobs'/row['job_id']/('guardian-'+str(row['attempt'])+'.json'),
            canonical_json({'pid': os.getpid(), 'group': group, 'attempt': row['attempt']}))
    queue.close()
    while True:
        try: os.kill(group, 0)
        except ProcessLookupError: break
        time.sleep(.25)
    while group_alive(group, exclude=(os.getpid(),)): time.sleep(.25)


def await_guardian(queue):
    row = queue.row(os.environ.get('HARNESS_INTEGRATION_JOB', ''))
    authorize(queue, row['operation'], row['owner'])
    ready = queue.home/'jobs'/row['job_id']/('guardian-'+str(row['attempt'])+'.json')
    deadline = time.monotonic()+5
    while time.monotonic() < deadline:
        try:
            data = load(ready)
            if data['attempt'] == row['attempt'] and os.getpgid(data['pid']) == data['group'] == os.getpgrp():
                return
        except (FileNotFoundError, ProcessLookupError): pass
        time.sleep(.02)
    raise QueueError('trusted execution lease guardian did not become ready')


def verifier_exec(queue, command):
    row = queue.row(os.environ.get('HARNESS_INTEGRATION_JOB', ''))
    authorize(queue, row['operation'], row['owner'])
    fds = [int(number) for number in os.environ['HARNESS_INTEGRATION_LEASES'].split(',')]
    env = {key: value for key, value in os.environ.items()
           if not key.startswith('HARNESS_INTEGRATION_') and key not in (
               'INTEGRATION_INPUT_DIR', 'HARNESS_SESSION_ID', 'HARNESS_SOURCE_ROOT',
               'HARNESS_SESSION_ROOT', 'HARNESS_RUN_DIR', 'HARNESS_SESSION_STATE_HOME')}
    queue.close()
    for fd in fds: os.close(fd)
    if command and command[0] == '--': command = command[1:]
    if not command: raise QueueError('verifier command is missing')
    os.execvpe(command[0], command, env)


def serve(queue, once=False):
    config = load(queue.home/'config.json')
    if not config.get('enabled'): raise QueueError('integration queue is disabled')
    if (type(config['jobs']) is not int or type(config['test_jobs']) is not int
            or not 1 <= config['jobs'] <= 4 or not 1 <= config['test_jobs'] <= 16
            or config['jobs'] * config['test_jobs'] > 16):
        raise QueueError('invalid integration execution budget')
    coordinator = lock(queue.home/'coordinator.lock')
    if coordinator is None: raise QueueError('integration coordinator is already owned (possibly by a surviving child)')
    gates = {}; running = {}; stopping = False; last_gate_report = 0

    def stop(_number, _frame):
        nonlocal stopping
        stopping = True

    previous = {number: signal.signal(number, stop) for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    try:
        while True:
            pools = [row[0] for row in queue.db.execute('SELECT state_root FROM pools')]
            for state in pools:
                if state not in gates:
                    queue.enrolled(Path(state))
                    fd = lock(Path(state)/'integration.lock')
                    if fd is not None: gates[state] = fd
            if len(gates) != len(pools) and time.monotonic()-last_gate_report >= 10:
                print(json.dumps({'event': 'waiting_for_legacy_gates', 'acquired': len(gates), 'required': len(pools)}), flush=True)
                last_gate_report = time.monotonic()
            for job, execution in list(running.items()):
                child = execution['child']
                if stopping and child.poll() is None:
                    try: os.killpg(child.pid, signal.SIGTERM)
                    except (ProcessLookupError, PermissionError): pass
                if child.poll() is not None:
                    if group_alive(child.pid):
                        # A dead broker shell is not a finished verification group.
                        started = execution.setdefault('cleanup_started', time.monotonic())
                        try: os.killpg(child.pid, signal.SIGKILL if time.monotonic()-started > 1 else signal.SIGTERM)
                        except (ProcessLookupError, PermissionError): pass
                        continue
                    if execution['lease'] is not None:
                        os.close(execution['lease']); execution['lease'] = None
                    # A detached descendant may still retain the inherited lease.
                    # Retain the budget slot until the kernel proves it ended.
                    probe = lock(execution['lease_path'])
                    if probe is None: continue
                    os.close(probe)
                    queue.finish(job, child.returncode)
                    print(json.dumps({'event': 'finished', **queue.status(job)}), flush=True)
                    execution['output'].close(); os.close(execution['pool_lease']); del running[job]
            if not stopping and len(gates) == len(pools):
                busy_destinations = {queue.row(job)['destination'] for job in running}
                barriers = set()
                rows = queue.db.execute("SELECT * FROM jobs WHERE state IN ('queued','running','needs_reconcile') ORDER BY sequence").fetchall()
                for entry in rows:
                    row = dict(entry); job = row['job_id']
                    is_gc = row['operation'] == 'gc'
                    if is_gc: barriers.add(row['state_root'])
                    elif row['state_root'] in barriers: continue
                    if job in running: continue
                    if len(running) >= config['jobs']: break
                    if row['destination'] in busy_destinations: continue
                    busy_destinations.add(row['destination'])
                    if row['attempt'] >= 2:
                        queue.db.execute("UPDATE jobs SET state='needs_reconcile',reason=? WHERE job_id=?",
                                         ('bounded recovery exhausted; inspect and explicitly reconcile the job', job)); queue.db.commit()
                        continue
                    state = Path(row['state_root'])
                    lease_path = state/'broker-maintenance.lock' if is_gc else state/'sessions'/row['owner']/'broker.lock'
                    pool_lease = lock(state/'queue-operations.lock', shared=not is_gc)
                    if pool_lease is None: continue
                    lease = lock(lease_path)
                    if lease is None:
                        os.close(pool_lease); continue
                    output = None
                    try:
                        engine = queue.verify_runtime(row['pin']); queue.verify_inputs(row)
                        attempt = row['attempt'] + 1
                        queue.db.execute("UPDATE jobs SET state='running',attempt=?,worker_pid=NULL,started=COALESCE(started,?),phase=CASE WHEN phase IN ('publishing','readback') THEN phase ELSE 'preparing' END,phase_started=?,finished=NULL WHERE job_id=?",
                                         (attempt, time.time(), time.time(), job)); queue.db.commit()
                        inherited = (coordinator, lease, gates[row['state_root']], pool_lease,
                                     *(gates[pool] for pool in sorted(gates) if pool != row['state_root']))
                        env = {k: v for k, v in os.environ.items() if k in ('HOME', 'PATH', 'USER', 'LOGNAME', 'LANG') or k.startswith('LC_')}
                        env.update(HARNESS_SESSION_STATE_HOME=row['state_root'], HARNESS_INTEGRATION_HOME=str(queue.home),
                                   HARNESS_INTEGRATION_JOB=job, HARNESS_INTEGRATION_ATTEMPT=str(attempt),
                                   HARNESS_INTEGRATION_LEASES=','.join(map(str, inherited)),
                                   HARNESS_DELIVER_TEST_JOBS=str(config['test_jobs']), HARNESS_TEST_CASE_JOBS_LIMIT=str(config['test_jobs']),
                                   PYTHONDONTWRITEBYTECODE='1', GIT_TERMINAL_PROMPT='0')
                        if is_gc:
                            arguments = json.loads(row['arguments'])
                            env.update(HARNESS_SESSION_RETENTION_SECONDS=str(arguments['retention']),
                                       HARNESS_GC_OWNER_FILTER=arguments.get('owner_filter', ''),
                                       HARNESS_GC_SOURCE_FILTER=arguments.get('source_filter', ''))
                        output = open(queue.home/'logs'/(job+'.log'), 'ab')
                        child = subprocess.Popen([str(engine), '_queue-execute', row['operation'], row['owner']],
                                  env=env, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                  pass_fds=inherited, start_new_session=True)
                        queue.db.execute('UPDATE jobs SET worker_pid=?,coordinator_pid=? WHERE job_id=?',
                                         (child.pid, os.getpid(), job)); queue.db.commit()
                        running[job] = dict(child=child, lease=lease, lease_path=lease_path, pool_lease=pool_lease, output=output)
                        print(json.dumps({'event': 'started', **queue.status(job)}), flush=True)
                    except (OSError, QueueError, ValueError) as exc:
                        queue.db.execute("UPDATE jobs SET state='failed',returncode=2,reason=?,finished=? WHERE job_id=?",
                                         (str(exc), time.time(), job)); queue.db.commit(); os.close(lease); os.close(pool_lease)
                        if output is not None: output.close()
            if stopping and not running: break
            if once and not running: break
            time.sleep(.1)
    finally:
        # Normal shutdown reaps exact owned children before releasing old-writer gates.
        for execution in running.values():
            child = execution['child']
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM); child.wait()
            execution['output'].close()
            if execution['lease'] is not None: os.close(execution['lease'])
            os.close(execution['pool_lease'])
        for fd in gates.values(): os.close(fd)
        os.close(coordinator)
        for number, handler in previous.items(): signal.signal(number, handler)


def wait(queue, job, timeout=None):
    if timeout is not None and (not math.isfinite(timeout) or timeout < 0):
        raise QueueError('wait timeout must be a finite nonnegative duration')
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        result = queue.status(job)
        if result['state'] not in ACTIVE:
            print(json.dumps(result)); return result['returncode'] or 0
        if deadline is not None and time.monotonic() >= deadline:
            print(json.dumps(result)); return 124
        time.sleep(.1)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', default=os.environ.get('HARNESS_SESSION_STATE_HOME'))
    parser.add_argument('--engine', default=str(Path(__file__).with_name('session-isolation.sh')))
    sub = parser.add_subparsers(dest='action', required=True)
    for action in ('enable', 'disable', 'enqueue', 'control', 'reconcile', 'status', 'wait', 'serve', 'authorize', 'phase', 'recovery-check', 'guardian', 'await-guardian', 'verifier-exec', 'service-plist', 'service-install'):
        command = sub.add_parser(action); command.add_argument('--home')
        if action == 'enable':
            command.add_argument('--jobs', type=int, default=1)
            command.add_argument('--test-jobs', type=int, default=1)
        elif action == 'enqueue': command.add_argument('owner')
        elif action == 'control':
            command.add_argument('operation', choices=('recover', 'discard', 'gc'))
            command.add_argument('owner', nargs='?', default=POOL_OWNER)
            command.add_argument('--retention', type=int, default=86400)
            command.add_argument('--filter-owner', default='')
            command.add_argument('--filter-source', default='')
            command.add_argument('--wait', action='store_true')
        elif action == 'reconcile': command.add_argument('job')
        elif action in ('status', 'wait'):
            command.add_argument('job')
            if action == 'wait': command.add_argument('--timeout', type=float)
        elif action == 'serve': command.add_argument('--once', action='store_true')
        elif action == 'authorize':
            command.add_argument('operation'); command.add_argument('owner')
        elif action == 'phase':
            command.add_argument('value', choices=('preparing', 'verifying', 'publishing', 'readback'))
        elif action == 'verifier-exec': command.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    state = Path(args.state) if args.state else Path.home()/'.local/state/harness-launcher'
    if args.action == 'enable' and not args.home:
        args.home = str(Path(os.environ.get('XDG_STATE_HOME', Path.home()/'.local/state'))/'harness-launcher/integration')
    queue = Queue(home_for(state, args.home))
    try:
        if args.action == 'enable':
            queue.enable(state, Path(args.engine), args.jobs, args.test_jobs)
            print(json.dumps({'enabled': True, 'home': str(queue.home)}))
        elif args.action == 'disable':
            queue.disable(); print(json.dumps({'enabled': False, 'home': str(queue.home)}))
        elif args.action == 'enqueue':
            job = queue.enqueue(state, args.owner, Path(args.engine))
            print(json.dumps({'accepted': True, 'job_id': job, 'state': queue.row(job)['state']}))
        elif args.action == 'status': print(json.dumps(queue.status(args.job)))
        elif args.action == 'wait': return wait(queue, args.job, args.timeout)
        elif args.action == 'control':
            if not 0 <= args.retention <= 604800: raise QueueError('invalid retention duration')
            if args.filter_owner and not UUID.fullmatch(args.filter_owner): raise QueueError('invalid GC owner')
            if bool(args.filter_owner) != bool(args.filter_source): raise QueueError('GC owner and source must be paired')
            if args.filter_source: absolute(args.filter_source)
            arguments = {'retention': args.retention, 'owner_filter': args.filter_owner, 'source_filter': args.filter_source} if args.operation == 'gc' else {}
            job = queue.control(state, args.owner, Path(args.engine), args.operation, arguments)
            if args.wait: return wait(queue, job)
            print(json.dumps({'accepted': True, 'job_id': job, 'state': 'queued'}))
        elif args.action == 'reconcile':
            row = queue.row(args.job)
            with acquired(Path(row['state_root'])/'sessions'/row['owner']/'broker.lock'):
                if row['operation'] != 'integrate' or row['state'] != 'needs_reconcile':
                    raise QueueError('only an indeterminate integration can be explicitly reconciled')
                queue.db.execute("UPDATE jobs SET attempt=0,reason=NULL WHERE job_id=?", (args.job,)); queue.db.commit()
            print(json.dumps(queue.status(args.job)))
        elif args.action == 'serve': serve(queue, args.once)
        elif args.action == 'authorize': print(authorize(queue, args.operation, args.owner))
        elif args.action == 'phase':
            job = os.environ.get('HARNESS_INTEGRATION_JOB', '')
            row = queue.row(job); authorize(queue, row['operation'], row['owner'])
            queue.db.execute('UPDATE jobs SET phase=?,phase_started=?,publish_intent=MAX(publish_intent,?) WHERE job_id=?',
                             (args.value, time.time(), int(args.value == 'publishing'), job)); queue.db.commit()
        elif args.action == 'recovery-check':
            row = queue.row(os.environ.get('HARNESS_INTEGRATION_JOB', ''))
            authorize(queue, row['operation'], row['owner'])
            record = Path(row['state_root'])/'sessions'/row['owner']
            if row['publish_intent'] and not all((record/name).is_file() for name in ('pending-sha', '.candidate-manifest')):
                raise QueueError('publish evidence is missing; preserve uncertainty and inspect remote history')
        elif args.action == 'guardian': guardian(queue)
        elif args.action == 'await-guardian': await_guardian(queue)
        elif args.action == 'verifier-exec': verifier_exec(queue, args.command)
        elif args.action == 'service-plist': sys.stdout.buffer.write(queue.service_plist())
        elif args.action == 'service-install': print(json.dumps({'installed': True, 'plist': str(queue.install_service())}))
        return 0
    finally: queue.close()


if __name__ == '__main__':
    try: sys.exit(main())
    except (QueueError, OSError, sqlite3.Error, ValueError) as exc:
        print('harness-session queue: ' + str(exc), file=sys.stderr); sys.exit(2)
