"""Closed projections for registered managed workspace-launch routes."""

import json
import re
import time
import uuid

import harness_herdr_local as local
import harness_herdr_launch_receipt as receipt

class LaunchError(Exception):
    pass


def _fail():
    raise LaunchError("invalid_response")


def _text(value, limit=256):
    if not isinstance(value, str) or not value or "\0" in value:
        return False
    try:
        return len(value.encode("utf-8")) <= limit and not any(ord(char) < 32 or 0x7f <= ord(char) <= 0x9f for char in value)
    except UnicodeEncodeError:
        return False


def catalog(client, device, local_device):
    path = "/api/workspace/launch-catalog" if device == local_device else f"/api/machines/{device}/workspace/launch-catalog"
    value = client.request_json(path)
    if not isinstance(value, dict) or set(value) - {"available", "reason", "revision", "profiles"} or type(value.get("available")) is not bool or not _text(value.get("revision")) or not isinstance(value.get("profiles"), list):
        _fail()
    profiles = []
    profile_ids = set()
    for profile in value["profiles"]:
        if not isinstance(profile, dict) or set(profile) != {"id", "label", "directory", "runtimes"} or not _text(profile["id"]) or not _text(profile["label"]) or not isinstance(profile["runtimes"], list):
            _fail()
        if profile["id"] in profile_ids:
            _fail()
        profile_ids.add(profile["id"])
        runtimes = []
        runtime_ids = set()
        for runtime in profile["runtimes"]:
            if not isinstance(runtime, dict) or set(runtime) - {"id", "label", "presets", "advanced"} or not {"id", "label", "presets"} <= set(runtime) or not isinstance(runtime["id"], str) or runtime["id"] not in {"claude", "codex"} or not _text(runtime["label"]) or not isinstance(runtime["presets"], list):
                _fail()
            if runtime["id"] in runtime_ids:
                _fail()
            runtime_ids.add(runtime["id"])
            presets = []
            preset_ids = set()
            for preset in runtime["presets"]:
                if not isinstance(preset, dict) or set(preset) != {"id", "label", "model", "effort"} or not all(_text(preset.get(key)) for key in ("id", "label", "model", "effort")):
                    _fail()
                if preset["id"] in preset_ids:
                    _fail()
                preset_ids.add(preset["id"])
                presets.append({key: preset[key] for key in ("id", "label", "model", "effort")})
            advanced = runtime.get("advanced")
            if advanced is not None:
                fields = {"models", "efforts", "efforts_by_model", "contexts", "permissions", "approvals", "sandboxes", "networks"}
                required = fields - {"efforts_by_model", "networks"}
                if not isinstance(advanced, dict) or set(advanced) - fields or not required <= set(advanced) or any(not isinstance(advanced[key], list) or any(not _text(item, 128) for item in advanced[key]) for key in set(advanced) - {"efforts_by_model"}):
                    _fail()
                if any(preset["model"] not in advanced["models"] for preset in presets):
                    _fail()
                by_model = advanced.get("efforts_by_model")
                if "efforts_by_model" in advanced and (not isinstance(by_model, dict) or set(by_model) != set(advanced["models"]) or any(not isinstance(model, str) or not isinstance(efforts, list) or any(not _text(effort, 128) or effort not in advanced["efforts"] for effort in efforts) for model, efforts in by_model.items())):
                    _fail()
                advanced = {key: advanced[key] for key in fields if key in advanced}
            runtimes.append({"id": runtime["id"], "label": runtime["label"], "presets": presets, **({"advanced": advanced} if advanced is not None else {})})
        profiles.append({"id": profile["id"], "label": profile["label"], "runtimes": runtimes})
    return {"device_id": device, "available": value["available"], "revision": value["revision"], "profiles": profiles}


def preview(client, device, local_device, request):
    current = catalog(client, device, local_device)
    _validate_selection(current, request)
    path = _path("launch-preview", device, local_device)
    value = client.post_json(path, request)
    return _preview_response(value, device, request)


def _validate_selection(current, request):
    if not current["available"] or request.get("catalog_revision") != current["revision"]:
        raise LaunchError("invalid_selection")
    profile = next((row for row in current["profiles"] if row["id"] == request["profile"]), None)
    runtime = next((row for row in profile["runtimes"] if row["id"] == request["runtime"]), None) if profile else None
    preset = next((row for row in runtime["presets"] if row["id"] == request["preset"]), None) if runtime else None
    if not runtime or not preset:
        raise LaunchError("invalid_selection")
    options = request.get("options", {})
    names = {"model": "models", "effort": "efforts", "context": "contexts", "permission": "permissions", "approval": "approvals", "sandbox": "sandboxes", "network": "networks"}
    advanced = runtime.get("advanced", {})
    if not isinstance(options, dict) or set(options) - set(names) or any(not isinstance(value, str) or value not in advanced.get(names[key], []) for key, value in options.items()):
        raise LaunchError("invalid_selection")
    if "effort" in options and options["effort"] not in advanced.get("efforts_by_model", {}).get(options.get("model", preset["model"]), advanced.get("efforts", [])):
        raise LaunchError("invalid_selection")
    return current


def _preview_response(value, device, request):
    options = request.get("options", {})
    if not isinstance(value, dict) or set(value) - {"catalog_revision", "plan_digest", "can_launch", "reason", "summary", "launcher_args"} or not {"catalog_revision", "plan_digest", "can_launch", "summary", "launcher_args"} <= set(value) or value["catalog_revision"] != request["catalog_revision"] or not isinstance(value["plan_digest"], str) or len(value["plan_digest"]) != 64 or any(char not in "0123456789abcdef" for char in value["plan_digest"]) or type(value["can_launch"]) is not bool or not isinstance(value["summary"], dict) or not isinstance(value["launcher_args"], list) or any(not isinstance(arg, str) for arg in value["launcher_args"]):
        _fail()
    if "reason" in value and not _text(value["reason"], 4096):
        _fail()
    required = {"runtime", "preset", "model", "effort", "context"}
    if set(value["summary"]) - (required | {"permission", "approval", "sandbox", "network", "action"}) or not required <= set(value["summary"]) or not all(_text(item, 256) for item in value["summary"].values()):
        _fail()
    if value["summary"]["runtime"] != request["runtime"] or value["summary"]["preset"] != request["preset"] or any(value["summary"].get(key) != option for key, option in options.items()):
        _fail()
    if "session" in request and value["summary"].get("action") != request["session"]["action"]:
        _fail()
    return {"device_id": device, "catalog_revision": value["catalog_revision"], "plan_digest": value["plan_digest"], "can_launch": value["can_launch"], "summary": value["summary"]}


def start(client, device, local_device, request, output, receipts=None):
    _validate_request(request)
    _validate_selection(catalog(client, device, local_device), request)
    intent = str(uuid.uuid4())
    store = receipt.LaunchReceiptStore() if receipts is None else receipts
    initial = store.create(intent, device, request["request_id"])
    record = store.read(intent)
    _emit(output, initial)
    value = client.post_json(_path("launch", device, local_device), request)
    job = _job(value, request["request_id"])
    record = store.transition(record, job["phase"])
    _emit(output, _job_public(record, job))
    return _observe(client, store, record, local_device, output)


def status(client, intent, local_device, output, receipts=None):
    store = receipt.LaunchReceiptStore() if receipts is None else receipts
    saved = store.read(intent)
    return _observe(client, store, saved, local_device, output)


def _observe(client, store, record, local_device, output):
    deadline = time.monotonic() + 240
    while True:
        if time.monotonic() >= deadline:
            result = store.transition(record, "unknown")
            result = _job_public(result, {"phase": "unknown"})
            _emit(output, result)
            return result
        try:
            value = _status_request(client, _path("launch-status", record["device_id"], local_device) + "?request_id=" + record["request_id"], deadline - time.monotonic())
            if time.monotonic() >= deadline:
                result = _job_public(store.transition(record, "unknown"), {"phase": "unknown"})
                _emit(output, result)
                return result
            job = _job(value, record["request_id"])
        except local.TransportError:
            result = _job_public(store.transition(record, "unknown"), {"phase": "unknown"})
            _emit(output, result)
            return result
        record = store.transition(record, job["phase"])
        result = _job_public(record, job)
        _emit(output, result)
        if result["phase"] in {"started", "failed", "unknown"}:
            return result
        if time.monotonic() >= deadline:
            result = _job_public(store.transition(record, "unknown"), {"phase": "unknown"})
            _emit(output, result)
            return result
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            result = _job_public(store.transition(record, "unknown"), {"phase": "unknown"})
            _emit(output, result)
            return result
        time.sleep(min(1, remaining))


def _path(name, device, local_device):
    return f"/api/workspace/{name}" if device == local_device else f"/api/machines/{device}/workspace/{name}"


def _validate_request(request):
    required = {"request_id", "catalog_revision", "profile", "runtime", "preset", "plan_digest"}
    allowed = required | {"options", "session"}
    if not isinstance(request, dict) or set(request) - allowed or set(request) != required | (set(request) & {"options", "session"}):
        raise LaunchError("invalid_selection")
    if not re.fullmatch(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}", request["request_id"]):
        raise LaunchError("invalid_selection")
    if not isinstance(request["plan_digest"], str) or re.fullmatch(r"[0-9a-f]{64}", request["plan_digest"]) is None:
        raise LaunchError("invalid_selection")
    try:
        encoded = json.dumps(request, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, UnicodeError, ValueError, RecursionError):
        raise LaunchError("invalid_selection") from None
    if len(encoded) > 4096:
        raise LaunchError("invalid_selection")


def _job(value, request_id):
    fields = {"request_id", "phase", "workspace_id", "pane_id", "session_id", "runtime_revision", "error"}
    phase = value.get("phase") if isinstance(value, dict) else None
    if not isinstance(value, dict) or set(value) - fields or value.get("request_id") != request_id or not isinstance(phase, str) or phase not in receipt.PHASES:
        _fail()
    if ("workspace_id" in value) != ("pane_id" in value):
        _fail()
    if "workspace_id" in value and (not _id(value["workspace_id"]) or not _id(value["pane_id"])):
        _fail()
    if "session_id" in value and not _id(value["session_id"]):
        _fail()
    if "runtime_revision" in value and not _text(value["runtime_revision"], 256):
        _fail()
    if "error" in value:
        error = value["error"]
        if not isinstance(error, dict) or set(error) != {"code", "message"} or not isinstance(error["code"], str) or not _text(error["message"], 4096):
            _fail()
    result = {"phase": phase}
    if phase == "started":
        if "workspace_id" not in value:
            _fail()
        result.update({"workspace_id": value["workspace_id"], "pane_id": value["pane_id"]})
    return result


def _id(value):
    return isinstance(value, str) and 0 < len(value) <= 256 and all(32 <= ord(char) < 127 for char in value)


def _emit(output, value):
    output.write(json.dumps(value, separators=(",", ":")) + "\n")
    output.flush()


def _job_public(record, job):
    result = {key: record[key] for key in ("intent", "device_id", "request_id", "phase")}
    if result["phase"] == "started":
        result.update({key: job[key] for key in ("workspace_id", "pane_id")})
    return result


def _status_request(client, path, remaining):
    if isinstance(client, local.Client):
        return client.request_json(path, deadline_seconds=min(10, remaining))
    return client.request_json(path)


def sessions(client, device, local_device):
    path = "/api/workspace/session-catalog" if device == local_device else f"/api/machines/{device}/workspace/session-catalog"
    value = client.request_json(path)
    fields = {"available", "revision", "status", "reason", "items", "problems"}
    if not isinstance(value, dict) or set(value) - fields or not {"available", "revision", "items"} <= set(value) or type(value["available"]) is not bool or not _text(value["revision"]) or not isinstance(value["items"], list):
        _fail()
    if "status" in value and (not isinstance(value["status"], str) or value["status"] not in {"ready", "partial", "unavailable"}):
        _fail()
    if "reason" in value and not _text(value["reason"], 4096):
        _fail()
    if "problems" in value and (not isinstance(value["problems"], list) or any(not isinstance(problem, dict) or set(problem) not in ({"profile", "code"}, {"profile", "kind"}) or not _text(problem.get("code", problem.get("kind")), 256) or (problem.get("profile") is not None and not _text(problem["profile"], 256)) for problem in value["problems"])):
        _fail()
    items = []
    item_ids = set()
    for row in value["items"]:
        required = {"id", "profile", "runtime", "native_id", "availability", "can_resume", "can_fork"}
        optional = {"launcher_session_id", "title", "reason", "source_root", "native_home", "transcript_path", "archived"}
        if not isinstance(row, dict) or set(row) - (required | optional) or not required <= set(row) or not all(_text(row[key], 256) for key in ("id", "profile", "native_id")) or not isinstance(row["runtime"], str) or row["runtime"] not in {"claude", "codex"} or not isinstance(row["availability"], str) or row["availability"] not in {"ready", "active", "archived", "legacy", "missing", "unavailable"} or type(row["can_resume"]) is not bool or type(row["can_fork"]) is not bool:
            _fail()
        if row["id"] in item_ids:
            _fail()
        item_ids.add(row["id"])
        if "launcher_session_id" in row and row["launcher_session_id"] is not None and not _text(row["launcher_session_id"], 256):
            _fail()
        if any(key in row and not _text(row[key], 4096) for key in ("title", "reason", "source_root", "native_home", "transcript_path")) or "archived" in row and type(row["archived"]) is not bool:
            _fail()
        items.append({key: row[key] for key in ("id", "profile", "runtime", "availability", "can_resume", "can_fork")})
    result = {"device_id": device, "available": value["available"], "revision": value["revision"], "items": items}
    if isinstance(value.get("status"), str): result["status"] = value["status"]
    return result
