"""Private, generation-CAS UI workflow receipts; never store answer material."""
import contextlib
import copy
import json
import os
from pathlib import Path
import secrets

import harness_herdr_guarded as guarded
import harness_herdr_store as private

MAX_U64 = (1 << 64) - 1
PHASES = {'prepare_started', 'known', 'step_started', 'uncertain', 'cancel_started'}
WORKFLOW_STATES = {'ready', 'awaiting_operation', 'stopped', 'completed_ui_sequence', 'cancelled', 'unknown'}

class ReceiptError(Exception):
    pass


def fail(code='receipt_invalid'):
    raise ReceiptError(code)


def canonical_uuid(value):
    try: private._uuid(value)
    except private.StoreError: fail()
    return value


def unsigned(value, maximum=MAX_U64):
    return type(value) is int and 0 <= value <= maximum


def workflow(value, epoch=None, workflow_id=None):
    required = {'workflow_id', 'server_epoch', 'state', 'next_step', 'step_count', 'revision'}
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - {'current_operation'}:
        fail()
    canonical_uuid(value['workflow_id'])
    if not guarded._text(value['server_epoch']) or epoch is not None and value['server_epoch'] != epoch or workflow_id is not None and value['workflow_id'] != workflow_id:
        fail()
    if not isinstance(value['state'], str) or value['state'] not in WORKFLOW_STATES or not unsigned(value['next_step'], 32) or not unsigned(value['step_count'], 32) or not unsigned(value['revision']):
        fail()
    if not 1 <= value['step_count'] or value['next_step'] > value['step_count'] or value['state'] == 'ready' and value['next_step'] >= value['step_count']:
        fail()
    if 'current_operation' in value:
        operation = value['current_operation']
        if not isinstance(operation, dict) or not {'operation_id', 'server_epoch', 'state', 'effect'} <= operation.keys(): fail()
        canonical_uuid(operation['operation_id'])
        try:
            guarded._safe({'local_intent_uuid': value['workflow_id'], 'state': 'unknown', 'operation': {'operation_uuid': operation['operation_id'], 'epoch': value['server_epoch']}}, operation)
        except guarded.GuardedError: fail()
    return copy.deepcopy(value)


def validate(record, expected=None):
    required = {'version', 'intent', 'device_id', 'server_epoch', 'workflow_id', 'local_generation', 'phase', 'attempt'}
    if not isinstance(record, dict) or not required <= record.keys() or record.keys() - required - {'last_result'}: fail()
    if type(record['version']) is not int or record['version'] != 1: fail()
    canonical_uuid(record['intent']); canonical_uuid(record['device_id'])
    if expected is not None and expected != record['intent']: fail()
    if not guarded._text(record['server_epoch']) or not unsigned(record['local_generation']) or not isinstance(record['phase'], str) or record['phase'] not in PHASES: fail()
    if record['workflow_id'] is not None: canonical_uuid(record['workflow_id'])
    if record['phase'] == 'known' and record['workflow_id'] is None: fail()
    attempt = record['attempt']
    if attempt is not None and (not isinstance(attempt, dict) or set(attempt) != {'step','revision'} or not unsigned(attempt['step'],32) or not unsigned(attempt['revision'])): fail()
    if record['phase'] == 'step_started' and attempt is None: fail()
    if record['phase'] == 'known' and attempt is not None: fail()
    if 'last_result' in record:
        if record['workflow_id'] is None: fail()
        workflow(record['last_result'], record['server_epoch'], record['workflow_id'])
    return record


class _Files(private.Store):
    """Reuse audited no-follow directory, regular-file, lock and bounded read code."""
    def __init__(self, home):
        super().__init__(home)
        self.root = self.home / '.local/state/harness-herdr-web/ui-answer-receipts/v1'

    @contextlib.contextmanager
    def open_verified_root(self):
        fds = []
        try:
            # Anchor every component, including home ancestors, instead of following
            # an intermediate symlink in a caller-supplied absolute home path.
            parts = self.home.parts
            if not self.home.is_absolute() or any(p in {'.','..'} for p in parts): fail('receipt_unsafe')
            fds.append(os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))
            for part in parts[1:]:
                fds.append(os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fds[-1]))
            info = os.fstat(fds[-1])
            if info.st_uid != self.uid or info.st_mode & 0o022: fail('receipt_unsafe')
            for part in ('.local', 'state', 'harness-herdr-web', 'ui-answer-receipts', 'v1'):
                fds.append(self._private_dir(fds[-1], part))
            yield fds[-1]
        finally:
            for fd in reversed(fds): os.close(fd)

    def _validate(self, record, expected=None):
        return validate(record, expected)

    def _read(self, root, intent):
        try: return super()._read(root, intent)
        except (UnicodeError, ValueError, TypeError, RecursionError): fail()

    def _write(self, root, record):
        validate(record)
        data = json.dumps(record, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()
        if len(data) > private.MAX_RECORD_BYTES: fail()
        temporary = '.ui-' + secrets.token_hex(24) + '.tmp'
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=root)
        try:
            if not private._private_regular(os.fstat(fd), self.uid): fail()
            view = memoryview(data)
            while view:
                count = os.write(fd, view)
                if count <= 0: raise OSError('write')
                view = view[count:]
            os.fsync(fd)
            os.close(fd); fd = None
            os.replace(temporary, self._record_name(record['intent']), src_dir_fd=root, dst_dir_fd=root)
            os.fsync(root)
        finally:
            if fd is not None: os.close(fd)
            try: os.unlink(temporary, dir_fd=root)
            except FileNotFoundError: pass


class UIReceiptStore:
    def __init__(self, home=None):
        self._files = _Files(Path.home() if home is None else Path(home))
        self.root = self._files.root

    def create(self, intent, device, epoch):
        record = validate({'version':1, 'intent':intent, 'device_id':device, 'server_epoch':epoch, 'workflow_id':None, 'local_generation':0, 'phase':'prepare_started', 'attempt':None})
        with self._files._locked_root(intent) as root:
            try: os.stat(intent+'.json', dir_fd=root, follow_symlinks=False)
            except FileNotFoundError: self._files._write(root, record)
            else: fail('receipt_exists')
        return record

    def read(self, intent):
        canonical_uuid(intent)
        with self._files._locked_root(intent) as root:
            return self._files._read(root, intent)

    def _marker_name(self, record):
        return canonical_uuid(record['intent']) + '.step-ban'

    def _marker_value(self, record):
        return {key: record[key] for key in ('device_id', 'server_epoch', 'workflow_id', 'local_generation')}

    def _marker_exists(self, root, record):
        try: os.stat(self._marker_name(record), dir_fd=root, follow_symlinks=False)
        except FileNotFoundError: return False
        return True

    def _reserve_marker(self, root, record):
        fd = os.open(self._marker_name(record), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=root)
        try:
            if not private._private_regular(os.fstat(fd), self._files.uid): fail()
            data = memoryview(json.dumps(self._marker_value(record), separators=(',', ':')).encode())
            while data:
                count = os.write(fd, data)
                if count <= 0: raise OSError('write')
                data = data[count:]
            os.fsync(fd)
        finally: os.close(fd)
        os.fsync(root)

    def _release_marker(self, root, reserved, unlink=True):
        # Only the original network reservation can remove its matching marker.
        # GET persistence and replacement receipt generations cannot release it.
        fd = os.open(self._marker_name(reserved), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root)
        try:
            if not private._private_regular(os.fstat(fd), self._files.uid): fail()
            raw = os.read(fd, private.MAX_RECORD_BYTES + 1)
            if len(raw) > private.MAX_RECORD_BYTES: fail()
            try: value = json.loads(raw.decode('utf-8'), object_pairs_hook=private._pairs)
            except (UnicodeError, ValueError, TypeError, RecursionError, private.StoreError): fail()
            if not isinstance(value, dict) or set(value) != {'device_id', 'server_epoch', 'workflow_id', 'local_generation'} or not unsigned(value.get('local_generation')): fail()
            if value != self._marker_value(reserved): fail('receipt_changed')
        finally: os.close(fd)
        if not unlink: return
        # The known result's file+directory fsync has already succeeded. Unlink
        # is the admission commit point; no fallible post-unlink fsync follows.
        # A crash may resurrect the marker, conservatively restoring the ban.
        os.unlink(self._marker_name(reserved), dir_fd=root)

    def transition(self, source, *, phase, workflow_id=None, last_result=None, attempt=None):
        validate(source)
        if source['local_generation'] == MAX_U64: fail('receipt_changed')
        value = copy.deepcopy(source)
        value.update(phase=phase, local_generation=source['local_generation']+1, attempt=attempt)
        if workflow_id is not None:
            if source['workflow_id'] is not None and source['workflow_id'] != workflow_id: fail('receipt_changed')
            value['workflow_id'] = workflow_id
        if last_result is not None: value['last_result'] = workflow(last_result, source['server_epoch'], value['workflow_id'])
        validate(value)
        with self._files._locked_root(source['intent']) as root:
            current = self._files._read(root, source['intent'])
            if current != source: fail('receipt_changed')
            reserve = phase == 'step_started' and source['phase'] == 'known'
            if reserve and self._marker_exists(root, source): fail('reconcile_only')
            release = source['phase'] == 'step_started' and phase == 'known'
            if release:
                if last_result is None or last_result['state'] != 'ready': fail()
                # Refuse a missing or unsafe reservation before publishing any
                # result that could otherwise permit a later step.
                self._release_marker(root, source, unlink=False)
            self._files._write(root, value)
            if reserve:
                self._reserve_marker(root, value)
            elif release:
                self._release_marker(root, source)
        return value
