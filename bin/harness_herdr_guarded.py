"""Private-file guarded input orchestration for the Herdr CLI."""
import json
import os
import stat
import uuid
import base64

import harness_herdr_files as files
import harness_herdr_store as store_module


METHOD = "pane.guarded_input.v1"
AGENT_METHOD = "agent.guarded_prompt.v1"
MAX_HTTP_BODY = 60000
KEYS = {"Enter", "Tab", "Escape", "Backspace", "Delete", "Up", "Down", "Left", "Right",
        "Home", "End", "Ctrl-C", "Ctrl-D"}
STATES = {"prepared", "queued", "writing_text", "waiting_enter", "writing_enter", "completed", "cancelled", "partial_unknown", "rejected"}
REASONS = {"guard_mismatch", "deadline_exceeded", "pty_closed", "io_error", "cancelled", "partial_unknown"}
REASON_STATES = {
    "guard_mismatch": {"rejected", "partial_unknown"},
    "deadline_exceeded": {"rejected", "partial_unknown"},
    "pty_closed": {"rejected", "partial_unknown"},
    "io_error": {"rejected", "partial_unknown"},
    "cancelled": {"cancelled"},
    "partial_unknown": {"partial_unknown"},
}


class GuardedError(Exception):
    pass


def _fail():
    raise GuardedError("invalid_guarded_request")


def _text(value, limit=256):
    try: return isinstance(value, str) and bool(value) and "\0" not in value and not any(ord(c) < 32 or 0x7f <= ord(c) <= 0x9f for c in value) and len(value.encode()) <= limit
    except UnicodeEncodeError: return False


def _target(value):
    required = {"server_epoch", "workspace_id", "tab_id", "pane_id", "terminal_id", "identity_generation", "agent_binding"}
    if not isinstance(value, dict) or set(value) != required or not all(_text(value[key]) for key in required - {"identity_generation", "agent_binding"}):
        _fail()
    if type(value["identity_generation"]) is not int or not 0 <= value["identity_generation"] <= (1 << 63) - 1:
        _fail()
    binding = value["agent_binding"]
    if not isinstance(binding, dict) or set(binding) != {"detected_agent", "hook", "session"}:
        _fail()
    if binding["detected_agent"] is not None and not _text(binding["detected_agent"]):
        _fail()
    _binding(binding["hook"], True); _binding(binding["session"], False)
    return value


def _binding(value, hook):
    if value is None: return
    keys = {"source", "agent_label", "session_ref"} if hook else {"source", "agent", "session_ref"}
    if not isinstance(value, dict) or set(value) != keys or not all(_text(value[k]) for k in keys - {"session_ref"}): _fail()
    ref = value["session_ref"]
    if hook and ref is None: return
    if not isinstance(ref, dict) or set(ref) != {"kind", "value"} or not isinstance(ref["kind"], str) or ref["kind"] not in {"id", "path"} or not _text(ref["value"], 512 if ref["kind"] == "id" else 4096) or ref["kind"] == "path" and not ref["value"].startswith("/"): _fail()


def _agent_target(value):
    target = _target(value)
    binding = target["agent_binding"]
    agent, hook, session = binding["detected_agent"], binding["hook"], binding["session"]
    if agent not in {"claude", "codex"} or not isinstance(hook, dict) or not isinstance(session, dict):
        _fail()
    if hook["agent_label"] != agent or session["agent"] != agent:
        _fail()
    return target


def read_target(path, device):
    wrapper = files.read_private_json_object(path, 16 * 1024)
    if not isinstance(wrapper, dict) or set(wrapper) != {"device_id", "target"} or wrapper["device_id"] != device:
        _fail()
    return _target(wrapper["target"])


def write_target(path, device, target):
    if not isinstance(path, str) or not path.startswith("/"):
        _fail()
    parts = path.split("/")[1:]
    if len(parts) < 2 or any(part in {"", ".", ".."} for part in parts):
        _fail()
    fds, fd = [os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)], None
    try:
        for part in parts[:-1]:
            fds.append(os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fds[-1]))
        parent = os.fstat(fds[-1])
        if parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) != 0o700:
            _fail()
        data = json.dumps({"device_id": device, "target": _target(target)}, separators=(",", ":")).encode()
        if len(data) > 16 * 1024:
            _fail()
        fd = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fds[-1])
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
            _fail()
        view = memoryview(data)
        while view:
            count = os.write(fd, view)
            if count <= 0: raise OSError("private_write")
            view = view[count:]
        os.fsync(fd); os.fsync(fds[-1])
    except (OSError, UnicodeError):
        _fail()
    finally:
        if fd is not None: os.close(fd)
        for directory in reversed(fds): os.close(directory)


def _input(kind, path_or_keys):
    if kind in {"text", "run"}:
        value = files.read_private_text(path_or_keys, MAX_HTTP_BODY)
        if not value or "\0" in value:
            _fail()
        return {"kind": kind, "text": value}
    values = path_or_keys.split(",") if isinstance(path_or_keys, str) else []
    if not 1 <= len(values) <= 32 or any(value not in KEYS for value in values):
        _fail()
    return {"kind": "keys", "keys": values}


def _body(target, input_value):
    value = {"server_epoch": target["server_epoch"], "target": target, "input": input_value}
    if len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()) > MAX_HTTP_BODY:
        _fail()
    return value


def _emit(stream, value):
    stream.write(json.dumps(value, separators=(",", ":")) + "\n")
    stream.flush()


def _safe(record, result=None):
    value = {"intent": record["local_intent_uuid"], "state": record["state"]}
    operation = record.get("operation")
    if operation:
        value.update({"operation_id": operation["operation_uuid"], "server_epoch": operation["epoch"]})
    if result is not None:
        if not isinstance(result, dict) or set(result) - {"operation_id", "server_epoch", "state", "effect", "reason"} or result.get("operation_id") != operation["operation_uuid"] or result.get("server_epoch") != operation["epoch"] or not isinstance(result.get("state"), str) or result["state"] not in STATES:
            _fail()
        if "effect" in result:
            effect = result["effect"]
            if not isinstance(effect, dict) or set(effect) != {"text_bytes_accepted", "enter_accepted"} or type(effect["text_bytes_accepted"]) is not int or not 0 <= effect["text_bytes_accepted"] <= (1 << 64) - 1 or type(effect["enter_accepted"]) is not bool:
                _fail()
            value["effect"] = effect
        else: _fail()
        if "reason" in result:
            if not isinstance(result["reason"], str) or result["reason"] not in REASONS or result["state"] not in REASON_STATES[result["reason"]]: _fail()
            value["reason"] = result["reason"]
        if result["state"] in {"prepared", "queued", "cancelled", "rejected"} and (effect["text_bytes_accepted"] != 0 or effect["enter_accepted"]): _fail()
        if isinstance(result.get("state"), str):
            value["state"] = result["state"]
    return value


def _prepared(value, epoch):
    keys = {"operation_id", "admission_ticket", "server_epoch", "deadline_monotonic_ms"}
    if not isinstance(value, dict) or set(value) != keys or value["server_epoch"] != epoch or type(value["deadline_monotonic_ms"]) is not int or not 0 <= value["deadline_monotonic_ms"] <= (1 << 64) - 1: _fail()
    try:
        if str(uuid.UUID(value["operation_id"])) != value["operation_id"] or not isinstance(value["admission_ticket"], str) or not value["admission_ticket"] or len(value["admission_ticket"]) > 4096 or base64.urlsafe_b64encode(base64.urlsafe_b64decode(value["admission_ticket"] + "==")).decode().rstrip("=") != value["admission_ticket"]: _fail()
    except Exception: _fail()
    return value


def run_input(client, store, device, target_file, payload, kind, stdout, local_device=None):
    target, input_value = read_target(target_file, device), _input(kind, payload)
    body, intent = _body(target, input_value), str(uuid.uuid4())
    identity = {key: target[key] for key in ("workspace_id", "tab_id", "pane_id", "terminal_id")}
    record = store.create_intent(intent, device, METHOD, safe_identity=identity)
    _emit(stdout, _safe(record))
    started = store.transition(intent, record["revision"], "prepare_started")
    prefix = "/api/cli/v1" if local_device in (None, device) else f"/api/machines/{device}/cli/v1"
    try:
        prepared = client.post_json(prefix + "/guarded/prepare", {"method": METHOD, "server_epoch": body["server_epoch"], "body": body})
    except Exception:
        store.transition(intent, started["revision"], "prepare_unknown")
        raise
    try: prepared = _prepared(prepared, body["server_epoch"])
    except GuardedError:
        store.transition(intent, started["revision"], "prepare_unknown"); raise
    bound = store.bind_operation(intent, started["revision"], prepared.get("server_epoch"), prepared.get("operation_id"), prepared.get("admission_ticket"))
    dispatched = store.transition(intent, bound["revision"], "dispatch_started")
    result = client.post_json(prefix + "/guarded/input", {**body, "admission_ticket": bound["operation"]["ticket"]})
    safe = _safe(dispatched, result)
    if result["state"] in {"completed", "cancelled", "partial_unknown", "rejected"}: store.observe_terminal(intent, dispatched["revision"])
    _emit(stdout, safe)
    return None


def run_agent_prompt(client, store, device, target_file, text_file, stdout, local_device=None):
    target = _agent_target(read_target(target_file, device))
    prompt = files.read_private_text(text_file, MAX_HTTP_BODY)
    if not prompt or "\0" in prompt:
        _fail()
    body = {"server_epoch": target["server_epoch"], "target": target, "prompt": prompt}
    if len(json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode()) > MAX_HTTP_BODY:
        _fail()
    intent = str(uuid.uuid4())
    identity = {key: target[key] for key in ("workspace_id", "tab_id", "pane_id", "terminal_id")}
    record = store.create_intent(intent, device, AGENT_METHOD, safe_identity=identity)
    _emit(stdout, _safe(record))
    started = store.transition(intent, record["revision"], "prepare_started")
    prefix = "/api/cli/v1" if local_device in (None, device) else f"/api/machines/{device}/cli/v1"
    try:
        prepared = client.post_json(prefix + "/agent/guarded/prepare", {"method": AGENT_METHOD, "server_epoch": body["server_epoch"], "body": body})
    except Exception:
        store.transition(intent, started["revision"], "prepare_unknown")
        raise
    try:
        prepared = _prepared(prepared, body["server_epoch"])
    except GuardedError:
        store.transition(intent, started["revision"], "prepare_unknown")
        raise
    bound = store.bind_operation(intent, started["revision"], prepared["server_epoch"], prepared["operation_id"], prepared["admission_ticket"])
    dispatched = store.transition(intent, bound["revision"], "dispatch_started")
    result = client.post_json(prefix + "/agent/guarded/prompt", {**body, "admission_ticket": bound["operation"]["ticket"]})
    safe = _safe(dispatched, result)
    if result["state"] in {"completed", "cancelled", "partial_unknown", "rejected"}:
        store.observe_terminal(intent, dispatched["revision"])
    _emit(stdout, safe)
    return None


def operation(client, store, intent, stdout, cancel=False, local_device=None):
    record = store.read(intent); op = record.get("operation")
    if not op: raise GuardedError("operation_unknown")
    device = record["device_uuid"]
    prefix = "/api/cli/v1" if local_device == device else f"/api/machines/{device}/cli/v1"
    suffix = "/guarded/operation/cancel" if cancel else "/guarded/operation"
    result = client.post_json(prefix + suffix, {"operation_id": op["operation_uuid"], "server_epoch": op["epoch"]})
    _emit(stdout, _safe(record, result))
    return None
