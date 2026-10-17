"""Read-only metadata commands for the fixed local herdr-web endpoint."""

import argparse
import json
import os
import stat
import re
import sys
import time
import uuid

import harness_herdr_local as local
import harness_herdr_guarded as guarded
import harness_herdr_store as store
import harness_herdr_files as files
import harness_herdr_launch as launch
import harness_herdr_launch_receipt as receipt
import harness_herdr_ui_answer as ui_answer
import harness_herdr_ui_receipt as ui_receipt
import harness_herdr_agent_ui_request as agent_ui_request
import harness_herdr_agent_ui_receipt as agent_ui_receipt
import harness_herdr_lifecycle as lifecycle
import harness_herdr_lifecycle_store as lifecycle_store
import harness_herdr_read as pane_read


MACHINE_STATES = ("connecting", "connected", "reconnecting", "disconnected", "error")


class CommandError(Exception):
    pass


def parser():
    result = argparse.ArgumentParser(prog="harness-herdr-web cli")
    sub = result.add_subparsers(dest="command", required=True)
    sub.add_parser("devices", help="list sanitized connected-device metadata")
    child = sub.add_parser("launch-catalog"); child.add_argument("--device", required=True)
    child = sub.add_parser("launch-sessions"); child.add_argument("--device", required=True)
    preview = sub.add_parser("launch-preview")
    for name in ("--device", "--catalog-revision", "--profile", "--runtime", "--preset"): preview.add_argument(name, required=True)
    for name in ("model", "effort", "context", "permission", "approval", "sandbox", "network"): preview.add_argument("--" + name)
    preview.add_argument("--session-id-file"); preview.add_argument("--session-action", choices=("resume", "fork"))
    managed = sub.add_parser("launch")
    for name in ("--device", "--catalog-revision", "--profile", "--runtime", "--preset", "--plan-digest"): managed.add_argument(name, required=True)
    for name in ("model", "effort", "context", "permission", "approval", "sandbox", "network"): managed.add_argument("--" + name)
    managed.add_argument("--session-id-file"); managed.add_argument("--session-action", choices=("resume", "fork"))
    managed.add_argument("--new-request", action="store_true", required=True)
    status = sub.add_parser("launch-status"); status.add_argument("--intent", required=True)
    wait = sub.add_parser("agent-wait", help="sample pane status", description="Sample pane status only: tuple-stable sessions are undetectable, and this proves neither turn/process exit nor close authority.")
    wait.add_argument("--device", required=True); wait.add_argument("--pane", required=True)
    wait.add_argument("--until", action="append", required=True); wait.add_argument("--timeout-seconds", required=True)
    for name in ("workspaces", "tabs", "panes", "agents"):
        child = sub.add_parser(name, help=f"list sanitized {name} metadata")
        child.add_argument("--device", required=True)
        if name in ("tabs", "panes"):
            child.add_argument("--workspace", required=True)
        if name == "panes":
            child.add_argument("--tab", required=True)
        if name == "agents":
            child.add_argument("--pane", required=True)
    target = sub.add_parser("guard-target")
    target.add_argument("--device", required=True); target.add_argument("--pane", required=True); target.add_argument("--out", required=True)
    for name, payload in (("input", "--text-file"), ("run", "--command-file"), ("keys", "--keys"), ("agent-prompt", "--text-file")):
        child = sub.add_parser(name); child.add_argument("--device", required=True); child.add_argument("--target-file", required=True)
        child.add_argument(payload, required=True); child.add_argument("--new-operation", action="store_true", required=True)
    for name in ("status", "cancel"):
        child = sub.add_parser(name); child.add_argument("--intent", required=True)
    prepare = sub.add_parser("ui-answer-prepare", allow_abbrev=False, help="prepare an ordinary Claude option workflow", description="Web screen-to-write residual applies. completed_ui_sequence means known PTY steps, with no provider acknowledgement. Recovery is same-operation get/cancel only; no automatic steps.")
    for name in ("device", "target-file", "prompt-file", "answer-file"): prepare.add_argument("--" + name, required=True)
    prepare.add_argument("--new-workflow", action="store_true", required=True)
    for name in ("step", "status", "cancel"):
        child = sub.add_parser("ui-answer-" + name, allow_abbrev=False)
        child.add_argument("--intent", required=True)
        if name == "step":
            child.add_argument("--expected-step", required=True)
            child.add_argument("--expected-revision", required=True)
    for name in ("agent-interrupt", "agent-exit"):
        child = sub.add_parser(name, allow_abbrev=False, help="request one observed Codex UI key; no exit or interruption completion claim")
        child.add_argument("--device", required=True); child.add_argument("--target-file", required=True)
        child.add_argument("--new-operation", action="store_true", required=True)
    for name in ("agent-ui-request-status", "agent-ui-request-cancel"):
        child = sub.add_parser(name, allow_abbrev=False); child.add_argument("--intent", required=True)
    for name in ("owned-create", "owned-interrupt", "owned-close"):
        child = sub.add_parser(name, allow_abbrev=False, help="perform one explicit owned lifecycle operation")
        if name == "owned-create":
            child.add_argument("--device", required=True); child.add_argument("--cwd-file", required=True)
        else: child.add_argument("--receipt", required=True)
        child.add_argument("--new-operation", action="store_true", required=True)
    for name in ("lifecycle-status", "lifecycle-cancel"):
        child = sub.add_parser(name, allow_abbrev=False)
        child.add_argument("--intent", required=True)
    child = sub.add_parser("read", allow_abbrev=False, help="read one pane's metadata; --body explicitly opts into bounded visible text")
    child.add_argument("--device", required=True); child.add_argument("--pane", required=True)
    child.add_argument("--body", action="store_true"); child.add_argument("--lines"); child.add_argument("--max-bytes")
    return result


def execute(argv, client):
    return execute_args(parser().parse_args(argv), client)


def execute_args(args, client, stdout=None):
    stdout = sys.stdout if stdout is None else stdout
    if args.command == "read": return pane_read.execute(args, client)
    if args.command in {"owned-create", "owned-interrupt", "owned-close", "lifecycle-status", "lifecycle-cancel"}:
        return lifecycle.execute(args, client, stdout)
    if args.command.startswith("ui-answer-"):
        return ui_answer.execute(args, client, stdout)
    if args.command in {"agent-interrupt", "agent-exit", "agent-ui-request-status", "agent-ui-request-cancel"}:
        return agent_ui_request.execute(args, client, stdout)
    if args.command in {"guard-target", "input", "run", "keys", "agent-prompt", "status", "cancel"}:
        if args.command in {"status", "cancel"} and receipt_namespace(args.intent) == "agent_ui_request":
            return agent_ui_request.execute(args, client, stdout)
        return guarded_execute(args, client)
    if args.command == "agent-wait":
        seconds = wait_options(args.pane, args.until, args.timeout_seconds)
        device = exact_uuid(args.device)
        deadline = time.monotonic() + seconds
        try:
            local_id = read_identity(client, observer_remaining(deadline))["device_id"]
        except (CommandError, local.TransportError) as error:
            observer_transport_error(error, deadline)
        return agent_wait(client, local_id, device, args.pane, args.until, deadline)
    identity = read_identity(client)
    if args.command == "launch-catalog":
        return launch.catalog(client, exact_uuid(args.device), identity["device_id"])
    if args.command == "launch-sessions":
        return launch.sessions(client, exact_uuid(args.device), identity["device_id"])
    if args.command == "launch-preview":
        request = managed_request(args, include_digest=False)
        return launch.preview(client, exact_uuid(args.device), identity["device_id"], request)
    if args.command == "launch":
        request = managed_request(args, include_digest=True)
        launch.start(client, exact_uuid(args.device), identity["device_id"], request, stdout)
        return None
    if args.command == "launch-status":
        try:
            intent = exact_uuid(args.intent)
        except CommandError:
            raise CommandError("invalid_selection") from None
        launch.status(client, intent, identity["device_id"], stdout)
        return None
    if args.command == "devices":
        return devices(client, identity["device_id"])
    target = exact_uuid(args.device)
    snapshot = read_snapshot(client, identity["device_id"], target)
    return project(args, snapshot)


def _receipt_present(path):
    """Missing is the only non-error absence; links and malformed inodes are fatal."""
    try:
        root = os.lstat(path.parent)
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(root.st_mode) or stat.S_ISLNK(root.st_mode) or root.st_uid != os.geteuid() or stat.S_IMODE(root.st_mode) != 0o700:
        raise CommandError("receipt_invalid")
    try:
        value = os.lstat(path)
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(value.st_mode) or stat.S_ISLNK(value.st_mode) or value.st_uid != os.geteuid() or stat.S_IMODE(value.st_mode) != 0o600 or value.st_nlink != 1:
        raise CommandError("receipt_invalid")
    return True


def receipt_namespace(intent):
    """Route legacy global status/cancel only when exactly one valid private receipt exists."""
    intent = exact_uuid(intent)
    candidates = []
    for name, database in (("guarded", store.Store()), ("agent_ui_request", agent_ui_receipt.Store())):
        path = database.root / (intent + ".json")
        if not _receipt_present(path):
            continue
        try:
            database.read(intent)
        except Exception as error:
            raise CommandError("receipt_invalid") from error
        candidates.append(name)
    if not candidates:
        # Preserve the established guarded command's own unknown-intent and
        # redaction behavior. There is no second valid receipt to infer from.
        return "guarded"
    if len(candidates) != 1:
        raise CommandError("receipt_invalid")
    return candidates[0]


def wait_options(pane, until, timeout):
    if not valid_id(pane) or not isinstance(timeout, str) or not timeout.isdecimal():
        raise CommandError("invalid_selection")
    digits = timeout.lstrip("0") or "0"
    if len(digits) > 3 or not 1 <= int(digits) <= 240: raise CommandError("invalid_selection")
    allowed = {"idle", "working", "blocked", "done", "unknown"}
    if not 1 <= len(until) <= 5 or len(set(until)) != len(until) or any(item not in allowed for item in until):
        raise CommandError("invalid_selection")
    return int(digits)


def observer_remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CommandError("wait_timeout")
    return remaining


def observer_transport_error(error, deadline):
    if str(error) in {"deadline_exceeded", "invalid_response"} and time.monotonic() >= deadline:
        raise CommandError("wait_timeout") from None
    raise error


def agent_wait(client, local_id, device, pane, until, deadline):
    allowed = {"idle", "working", "blocked", "done", "unknown"}
    captured = None
    for _ in range(240):
        try:
            snapshot = read_snapshot(client, local_id, device, observer_remaining(deadline))
        except (CommandError, local.TransportError) as error:
            observer_transport_error(error, deadline)
        if not snapshot["server_epoch"]: raise CommandError("epoch_unavailable")
        rows = [row for row in snapshot["panes"] if row["pane_id"] == pane]
        if not rows: raise CommandError("target_changed" if captured is not None else "target_not_found")
        if len(rows) != 1: raise CommandError("invalid_response")
        row = rows[0]
        if row["agent"] is None: raise CommandError("target_changed" if captured is not None else "agent_unavailable")
        if row["agent_status"] not in allowed: raise CommandError("invalid_response")
        tuple_now = tuple(row[key] for key in ("workspace_id", "tab_id", "pane_id", "terminal_id", "agent")) + (snapshot["device_id"], snapshot["server_epoch"])
        if captured is None: captured = tuple_now
        elif captured != tuple_now: raise CommandError("target_changed")
        observer_remaining(deadline)
        if row["agent_status"] in until:
            return {"device_id": snapshot["device_id"], "server_epoch": snapshot["server_epoch"], "pane_id": pane, "terminal_id": row["terminal_id"], "agent": row["agent"], "agent_status": row["agent_status"], "observation": "status_matched"}
        time.sleep(min(1, observer_remaining(deadline)))
    raise CommandError("wait_timeout")


def managed_request(args, include_digest):
    request = {"request_id": str(uuid.uuid4()), "catalog_revision": args.catalog_revision,
               "profile": args.profile, "runtime": args.runtime, "preset": args.preset}
    if include_digest:
        request["plan_digest"] = args.plan_digest
    options = {key: getattr(args, key) for key in ("model", "effort", "context", "permission", "approval", "sandbox", "network") if getattr(args, key) is not None}
    if options:
        request["options"] = options
    if bool(args.session_id_file) != bool(args.session_action):
        raise CommandError("invalid_selection")
    if args.session_id_file:
        session_id = files.read_private_text(args.session_id_file, 128)
        if not re.fullmatch(r"[0-9a-f]{64}", session_id):
            raise CommandError("invalid_selection")
        request["session"] = {"id": session_id, "action": args.session_action}
    return request


def guarded_execute(args, client, stdout=None):
    stdout = sys.stdout if stdout is None else stdout
    if args.command == "read": return pane_read.execute(args, client)
    local_id = read_identity(client)["device_id"]
    if args.command == "guard-target":
        device = exact_uuid(args.device)
        if not valid_id(args.pane): raise CommandError("invalid_target")
        prefix = "/api/cli/v1" if device == local_id else f"/api/machines/{device}/cli/v1"
        value = client.post_json(prefix + "/guarded/target", {"pane_id": args.pane})
        if not isinstance(value, dict) or set(value) != {"target"}: raise CommandError("invalid_response")
        if not isinstance(value["target"], dict) or value["target"].get("pane_id") != args.pane: raise CommandError("invalid_response")
        guarded.write_target(args.out, device, value["target"]); return None
    if args.command == "agent-prompt":
        return guarded.run_agent_prompt(client, store.Store(), exact_uuid(args.device), args.target_file,
                                        args.text_file, stdout, local_id)
    if args.command in {"input", "run", "keys"}:
        payload = getattr(args, "text_file", None) or getattr(args, "command_file", None) or args.keys
        return guarded.run_input(client, store.Store(), exact_uuid(args.device), args.target_file, payload,
                                 "text" if args.command == "input" else args.command, stdout, local_id)
    return guarded.operation(client, store.Store(), args.intent, stdout, args.command == "cancel", local_id)


def read_identity(client, deadline_seconds=None):
    try:
        payload = get_json(client, "/api/cli/v1/identity", deadline_seconds)
    except local.TransportError as error:
        if str(error) == "http_404":
            raise CommandError("capability_unsupported") from None
        raise
    if not isinstance(payload, dict) or payload.get("protocol") != 1:
        raise CommandError("invalid_response")
    device_id = response_uuid(payload.get("device_id"))
    caps = payload.get("capabilities")
    native = payload.get("native")
    if not isinstance(caps, dict) or caps.get("read_metadata_v1") is not True or not isinstance(caps.get("guarded_mutation_v1"), bool):
        raise CommandError("capability_unsupported")
    if not isinstance(native, dict) or not isinstance(native.get("version"), (str, type(None))):
        raise CommandError("invalid_response")
    return {"device_id": device_id}


def devices(client, local_id):
    payload = get_json(client, "/api/machines")
    machines = payload.get("machines") if isinstance(payload, dict) else None
    if not isinstance(machines, list):
        raise CommandError("invalid_response")
    return {"devices": [project_device(row, local_id) for row in machines if valid_device(row, local_id)]}


def valid_device(row, local_id):
    if not isinstance(row, dict) or not isinstance(row.get("name"), str) or not isinstance(row.get("state"), str):
        return False
    try:
        response_uuid(local_id if row.get("id") == "local" else row.get("id"))
    except CommandError:
        return False
    return device_label(row["name"]) and row["state"] in MACHINE_STATES and (row.get("role") is None or row.get("role") in ("watch", "drive"))


def project_device(row, local_id):
    value = {"id": local_id if row["id"] == "local" else response_uuid(row["id"]), "name": row["name"], "state": row["state"]}
    return value if row.get("role") is None else {**value, "role": row["role"]}


def read_snapshot(client, local_id, target, deadline_seconds=None):
    path = "/api/cli/v1/snapshot" if target == local_id else f"/api/machines/{target}/cli/v1/snapshot"
    payload = get_json(client, path, deadline_seconds)
    if not isinstance(payload, dict) or payload.get("protocol") != 1 or response_uuid(payload.get("device_id")) != target:
        raise CommandError("invalid_response")
    if "server_epoch" not in payload or not isinstance(payload["server_epoch"], (str, type(None))):
        raise CommandError("invalid_response")
    if not all(isinstance(payload.get(name), list) for name in ("workspaces", "tabs", "panes")):
        raise CommandError("invalid_response")
    validate_snapshot(payload)
    return payload


def project(args, snapshot):
    fields = {"device_id": snapshot["device_id"], "server_epoch": snapshot["server_epoch"]}
    if args.command == "workspaces":
        return {**fields, "workspaces": ids(snapshot["workspaces"], "workspace_id")}
    if args.command == "tabs":
        require_member(snapshot["workspaces"], "workspace_id", args.workspace)
        return {**fields, "tabs": linked(snapshot["tabs"], "workspace_id", args.workspace, ("tab_id", "workspace_id"))}
    if args.command == "panes":
        require_member(snapshot["workspaces"], "workspace_id", args.workspace)
        require_tab(snapshot["tabs"], args.workspace, args.tab)
        return {**fields, "panes": panes(snapshot["panes"], args.workspace, args.tab)}
    require_member(snapshot["panes"], "pane_id", args.pane)
    return {**fields, "agents": agents(snapshot["panes"], args.pane)}


def ids(rows, key):
    return [{key: row[key]} for row in rows if isinstance(row, dict) and valid_id(row.get(key))]


def linked(rows, link, value, fields):
    return [{field: row[field] for field in fields} for row in rows if isinstance(row, dict) and row.get(link) == value and all(valid_id(row.get(field)) for field in fields)]


def panes(rows, workspace, tab):
    allowed = ("pane_id", "terminal_id", "workspace_id", "tab_id", "agent", "agent_status")
    return [{field: row[field] for field in allowed if field in row} for row in rows if valid_pane(row, workspace, tab)]


def valid_pane(row, workspace, tab):
    fields = ("pane_id", "terminal_id", "workspace_id", "tab_id", "agent", "agent_status")
    return isinstance(row, dict) and all(field in row for field in fields) and row["workspace_id"] == workspace and row["tab_id"] == tab and all(valid_id(row[field]) for field in ("pane_id", "terminal_id", "workspace_id", "tab_id")) and (row["agent"] is None or safe_text(row["agent"])) and safe_text(row["agent_status"])


def agents(rows, pane):
    result = [row for row in rows if isinstance(row, dict) and row.get("pane_id") == pane and valid_pane(row, row.get("workspace_id"), row.get("tab_id"))]
    return [{"pane_id": row["pane_id"], "agent": row["agent"], "agent_status": row["agent_status"]} for row in result]


def valid_id(value):
    return isinstance(value, str) and bool(value) and len(value) <= 256 and all(32 <= ord(char) < 127 for char in value)


def safe_text(value):
    return isinstance(value, str) and len(value) <= 256 and all(32 <= ord(char) < 127 for char in value)


def device_label(value):
    return isinstance(value, str) and len(value) <= 256 and (not value or value.isprintable())


def exact_uuid(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}", value):
        raise CommandError("invalid_device_id") from None
    return value


def response_uuid(value):
    try:
        return exact_uuid(value)
    except CommandError:
        raise CommandError("invalid_response") from None


def get_json(client, path, deadline_seconds=None):
    try:
        if deadline_seconds is None:
            return client.request_json(path)
        return client.request_json(path, deadline_seconds)
    except local.TransportError as error:
        if str(error) == "invalid_response":
            raise CommandError("invalid_response") from None
        raise


def validate_snapshot(snapshot):
    workspace_ids = set()
    for row in snapshot["workspaces"]:
        if not isinstance(row, dict) or not valid_id(row.get("workspace_id")):
            raise CommandError("invalid_response")
        workspace_ids.add(row["workspace_id"])
    tab_pairs = set()
    for row in snapshot["tabs"]:
        if not isinstance(row, dict) or not all(valid_id(row.get(key)) for key in ("tab_id", "workspace_id")):
            raise CommandError("invalid_response")
        if row["workspace_id"] not in workspace_ids:
            raise CommandError("invalid_response")
        tab_pairs.add((row["workspace_id"], row["tab_id"]))
    for row in snapshot["panes"]:
        if not isinstance(row, dict) or not valid_pane(row, row.get("workspace_id"), row.get("tab_id")):
            raise CommandError("invalid_response")
        if row["workspace_id"] not in workspace_ids or (row["workspace_id"], row["tab_id"]) not in tab_pairs:
            raise CommandError("invalid_response")


def require_member(rows, field, value):
    if not any(row[field] == value for row in rows):
        raise CommandError("target_not_found")


def require_tab(rows, workspace, tab):
    if not any(row["workspace_id"] == workspace and row["tab_id"] == tab for row in rows):
        raise CommandError("target_not_found")


def main(argv):
    args = parser().parse_args(argv)
    try:
        result = execute_args(args, client=local.discover(), stdout=sys.stdout)
        if result is not None:
            print(json.dumps(result, separators=(",", ":")))
        return 0
    except (CommandError, local.DiscoveryError, local.TransportError, launch.LaunchError, ui_answer.UIAnswerError, ui_receipt.ReceiptError, agent_ui_request.RequestError, agent_ui_receipt.ReceiptError, lifecycle.LifecycleError, lifecycle_store.LifecycleStoreError, pane_read.ReadError) as error:
        print(json.dumps({"error": {"code": str(error)}}))
        return 1
    except KeyboardInterrupt:
        print(json.dumps({"error": {"code": "wait_cancelled"}}))
        return 1
    except guarded.GuardedError as error:
        code = "operation_unknown" if str(error) == "operation_unknown" else "guarded_request_failed"
        print(json.dumps({"error": {"code": code}}))
        return 1
    except (store.StoreError, receipt.ReceiptError, files.FileError, OSError):
        print(json.dumps({"error": {"code": "guarded_request_failed"}}))
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
