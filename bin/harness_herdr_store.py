"""Private filesystem-only write-ahead store for guarded Herdr intents."""
import base64
import contextlib
import fcntl
import json
import os
import pwd
import secrets
import stat
import uuid
from pathlib import Path

MAX_RECORD_BYTES = 64 * 1024
MAX_U64 = (1 << 63) - 1
METHODS = {"lifecycle.guarded_create.v1", "pane.guarded_input.v1",
           "agent.guarded_prompt.v1", "lifecycle.guarded_interrupt.v1",
           "lifecycle.guarded_close.v1"}
STATES = {"intent_durable", "prepare_started", "prepare_unknown",
          "prepared_durable", "dispatch_started", "observed_terminal", "unknown"}
IDENTITY_KEYS = {"workspace_id", "tab_id", "pane_id", "terminal_id"}


class StoreError(Exception):
    pass


class StoreBusy(StoreError):
    pass


def _fail():
    raise StoreError("private_store_invalid")


def _uuid(value):
    if not isinstance(value, str):
        _fail()
    try:
        if str(uuid.UUID(value)) != value:
            _fail()
    except (ValueError, AttributeError):
        _fail()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail()
        result[key] = value
    return result


def _private_regular(info, uid):
    return (stat.S_ISREG(info.st_mode) and info.st_uid == uid and info.st_nlink == 1
            and stat.S_IMODE(info.st_mode) == 0o600)


def _text(value, limit):
    if not isinstance(value, str) or "\0" in value:
        return False
    try:
        return len(value.encode("utf-8")) <= limit
    except UnicodeEncodeError:
        return False


class Store:
    def __init__(self, home=None):
        self.uid = os.geteuid()
        home = pwd.getpwuid(self.uid).pw_dir if home is None else os.fspath(home)
        self.home = Path(home)
        self.root = self.home / ".local" / "state" / "harness-herdr-web" / "v1"

    def _dir(self, name, parent=None, private=False):
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        try:
            fd = os.open(name, flags) if parent is None else os.open(name, flags, dir_fd=parent)
        except FileNotFoundError:
            raise
        except OSError:
            _fail()
        info = os.fstat(fd)
        unsafe = not stat.S_ISDIR(info.st_mode) or info.st_uid != self.uid
        unsafe |= (stat.S_IMODE(info.st_mode) != 0o700 if private else bool(info.st_mode & 0o022))
        if unsafe:
            os.close(fd)
            _fail()
        return fd

    def _private_dir(self, parent, name):
        try:
            fd = self._dir(name, parent, private=True)
            try:
                os.fsync(parent)
            except Exception:
                os.close(fd)
                raise
            return fd
        except FileNotFoundError:
            os.mkdir(name, 0o700, dir_fd=parent)
            fd = self._dir(name, parent, private=True)
            if stat.S_IMODE(os.fstat(fd).st_mode) != 0o700:
                os.close(fd)
                _fail()
            try:
                os.fsync(parent)
            except Exception:
                os.close(fd)
                raise
            return fd

    @contextlib.contextmanager
    def open_verified_root(self):
        fds = []
        try:
            fds.append(self._dir(os.fspath(self.home)))
            fds.append(self._dir(".local", fds[-1]))
            fds.append(self._dir("state", fds[-1]))
            fds.append(self._private_dir(fds[-1], "harness-herdr-web"))
            fds.append(self._private_dir(fds[-1], "v1"))
            yield fds[-1]
        finally:
            for fd in reversed(fds):
                os.close(fd)

    def _record_name(self, intent_uuid):
        _uuid(intent_uuid)
        return intent_uuid + ".json"

    def _lock_name(self, intent_uuid):
        _uuid(intent_uuid)
        return intent_uuid + ".lock"

    def _operation(self, value):
        if not isinstance(value, dict) or set(value) != {"epoch", "operation_uuid", "ticket"}:
            _fail()
        if not _text(value["epoch"], 256) or not _text(value["ticket"], 4096):
            _fail()
        _uuid(value["operation_uuid"])

    def _receipt(self, value):
        if not isinstance(value, dict) or set(value) != {"receipt_id", "generation", "resource_ids"}:
            _fail()
        if not _text(value["receipt_id"], 256) or type(value["generation"]) is not int:
            _fail()
        if not 0 <= value["generation"] <= MAX_U64:
            _fail()
        ids = value["resource_ids"]
        if not isinstance(ids, dict) or set(ids) != IDENTITY_KEYS or any(not _text(v, 256) for v in ids.values()):
            _fail()

    def _validate(self, record, expected=None):
        required = {"version", "revision", "local_intent_uuid", "device_uuid", "method", "state", "safe_identity"}
        allowed = required | {"owner_key", "operation", "create_receipt"}
        if not isinstance(record, dict) or not required <= set(record) or set(record) - allowed:
            _fail()
        if type(record["version"]) is not int or record["version"] != 1:
            _fail()
        if type(record["revision"]) is not int or not 0 <= record["revision"] <= MAX_U64:
            _fail()
        _uuid(record["local_intent_uuid"])
        _uuid(record["device_uuid"])
        if expected is not None and record["local_intent_uuid"] != expected:
            _fail()
        if not isinstance(record["method"], str) or not isinstance(record["state"], str):
            _fail()
        if record["method"] not in METHODS or record["state"] not in STATES:
            _fail()
        identity = record["safe_identity"]
        if not isinstance(identity, dict) or set(identity) - IDENTITY_KEYS or any(not _text(v, 256) for v in identity.values()):
            _fail()
        create = record["method"] == "lifecycle.guarded_create.v1"
        if create != ("owner_key" in record):
            _fail()
        if create:
            key = record["owner_key"]
            if not isinstance(key, str) or "=" in key:
                _fail()
            try:
                raw = base64.urlsafe_b64decode(key + "===")
            except Exception:
                _fail()
            if len(raw) != 32 or base64.urlsafe_b64encode(raw).rstrip(b"=").decode() != key:
                _fail()
        has_operation = "operation" in record
        if has_operation:
            self._operation(record["operation"])
        if record["state"] in {"prepared_durable", "dispatch_started", "observed_terminal"} and not has_operation:
            _fail()
        if record["state"] == "prepare_unknown" and has_operation:
            _fail()
        if "create_receipt" in record:
            if not create or record["state"] != "observed_terminal":
                _fail()
            self._receipt(record["create_receipt"])
        return record

    def _read(self, root, intent_uuid):
        try:
            fd = os.open(self._record_name(intent_uuid), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root)
        except OSError:
            _fail()
        try:
            info = os.fstat(fd)
            if not _private_regular(info, self.uid) or info.st_size > MAX_RECORD_BYTES:
                _fail()
            data = b""
            while len(data) <= MAX_RECORD_BYTES:
                piece = os.read(fd, min(8192, MAX_RECORD_BYTES + 1 - len(data)))
                if not piece:
                    break
                data += piece
        finally:
            os.close(fd)
        if len(data) > MAX_RECORD_BYTES or b"\0" in data:
            _fail()
        try:
            return self._validate(json.loads(data.decode("utf-8"), object_pairs_hook=_pairs), intent_uuid)
        except (UnicodeDecodeError, json.JSONDecodeError, StoreError):
            _fail()

    def _write(self, root, record):
        self._validate(record, record["local_intent_uuid"])
        data = json.dumps(record, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(data) > MAX_RECORD_BYTES or b"\0" in data:
            _fail()
        temporary = ".intent-" + secrets.token_hex(24) + ".tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=root)
        try:
            if not _private_regular(os.fstat(fd), self.uid):
                _fail()
            view = memoryview(data)
            while view:
                count = os.write(fd, view)
                if count <= 0:
                    raise OSError("private write failed")
                view = view[count:]
            os.fsync(fd)
            os.close(fd)
            fd = None
            os.replace(temporary, self._record_name(record["local_intent_uuid"]), src_dir_fd=root, dst_dir_fd=root)
            os.fsync(root)
        except Exception:
            if fd is not None:
                os.close(fd)
            try:
                os.unlink(temporary, dir_fd=root)
            except OSError:
                pass
            raise

    @contextlib.contextmanager
    def _locked_root(self, intent_uuid):
        name = self._lock_name(intent_uuid)
        with self.open_verified_root() as root:
            fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=root)
            try:
                if not _private_regular(os.fstat(fd), self.uid):
                    _fail()
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise StoreBusy("busy")
                yield root
            finally:
                os.close(fd)

    @contextlib.contextmanager
    def lock(self, intent_uuid):
        with self._locked_root(intent_uuid):
            yield

    def read(self, intent_uuid):
        self._record_name(intent_uuid)
        with self.open_verified_root() as root:
            return self._read(root, intent_uuid)

    def create_intent(self, intent_uuid, device_uuid, method, create=False, safe_identity=None):
        self._record_name(intent_uuid)
        _uuid(device_uuid)
        if create != (method == "lifecycle.guarded_create.v1"):
            _fail()
        record = {"version": 1, "revision": 0, "local_intent_uuid": intent_uuid,
                  "device_uuid": device_uuid, "method": method, "state": "intent_durable",
                  "safe_identity": {} if safe_identity is None else safe_identity}
        if create:
            record["owner_key"] = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
        self._validate(record, intent_uuid)
        with self._locked_root(intent_uuid) as root:
            try:
                os.stat(self._record_name(intent_uuid), dir_fd=root, follow_symlinks=False)
            except FileNotFoundError:
                self._write(root, record)
            else:
                raise StoreError("private_store_exists")
        return record

    def transition(self, intent_uuid, revision, state):
        allowed = {"intent_durable": {"prepare_started"}, "prepare_started": {"prepare_unknown"},
                   "prepared_durable": {"dispatch_started"}}
        if type(revision) is not int or not isinstance(state, str) or state not in STATES:
            _fail()
        with self._locked_root(intent_uuid) as root:
            record = self._read(root, intent_uuid)
            if record["revision"] != revision:
                raise StoreError("private_store_conflict")
            if state not in allowed.get(record["state"], set()):
                raise StoreError("reconcile_only")
            record["state"], record["revision"] = state, revision + 1
            self._write(root, record)
            return record

    def bind_operation(self, intent_uuid, revision, epoch, operation_uuid, ticket):
        if type(revision) is not int:
            _fail()
        operation = {"epoch": epoch, "operation_uuid": operation_uuid, "ticket": ticket}
        self._operation(operation)
        with self._locked_root(intent_uuid) as root:
            record = self._read(root, intent_uuid)
            if record["revision"] != revision or record["state"] != "prepare_started" or "operation" in record:
                raise StoreError("reconcile_only")
            record["operation"], record["state"], record["revision"] = operation, "prepared_durable", revision + 1
            self._write(root, record)
            return record

    def observe_terminal(self, intent_uuid, revision, safe_identity=None, create_receipt=None, unknown=False):
        if type(revision) is not int or type(unknown) is not bool:
            _fail()
        with self._locked_root(intent_uuid) as root:
            record = self._read(root, intent_uuid)
            if record["revision"] != revision or record["state"] not in {"prepared_durable", "dispatch_started", "unknown"}:
                raise StoreError("reconcile_only")
            if record["state"] == "unknown" and "operation" not in record:
                raise StoreError("reconcile_only")
            record["state"], record["revision"] = ("unknown" if unknown else "observed_terminal"), revision + 1
            if safe_identity is not None:
                record["safe_identity"] = safe_identity
            if create_receipt is not None:
                record["create_receipt"] = create_receipt
            self._write(root, record)
            return record

    def reconcile_only(self, intent_uuid):
        return self.read(intent_uuid)["state"] in {"prepare_started", "prepare_unknown", "prepared_durable", "dispatch_started", "unknown"}
