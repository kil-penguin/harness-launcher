#!/usr/bin/env python3
"""Pure, validated launch plans shared by terminal and Herdr Web callers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
import tomllib
from pathlib import Path
from typing import Any


MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*(?:\[1m\])?$")
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$")
EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max", "ultra")
CLAUDE_PERMISSIONS = ("default", "acceptEdits", "dontAsk", "plan", "bypassPermissions")
CODEX_APPROVALS = ("on-request", "never")
CODEX_SANDBOXES = ("read-only", "workspace-write", "danger-full-access")
CODEX_NETWORKS = ("enabled", "disabled")
SETTING_KEYS = frozenset({"model", "effort", "context", "permission", "approval", "sandbox", "network"})
SESSION_KEYS = frozenset({"native_id", "action", "runtime", "launcher_session_id", "source_root", "native_home", "archived"})
GRANT_KEYS = frozenset({"permission", "approval", "sandbox", "network", "bypass", "source_root", "isolated", "harness_session_id", "context", "grant_provenance"})


class PlanError(ValueError):
    """A stable, non-sensitive validation failure for plan callers."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def network_from_overrides(overrides: list[str]) -> str:
    """Recognize only the exact network boolean; ambiguous replacements clear it."""
    selected = ""
    for override in overrides:
        key, separator, value = override.partition("=")
        key = key.strip()
        if "sandbox_workspace_write" not in key:
            continue
        if key == "sandbox_workspace_write.network_access" and separator:
            selected = ""
            try:
                parsed = tomllib.loads("value=" + value)["value"]
            except (tomllib.TOMLDecodeError, KeyError):
                continue
            if type(parsed) is bool:
                selected = "enabled" if parsed else "disabled"
        elif key.replace('"', '').replace("'", '') == "sandbox_workspace_write" or "network_access" in key:
            selected = ""
    return selected


def _fail(code: str) -> None:
    raise PlanError(code)


def _source_root(source: Path) -> Path:
    if not isinstance(source, Path):
        _fail("invalid_source")
    try:
        source_stat = source.lstat()
        resolved = source.resolve(strict=True)
        resolved_stat = resolved.stat()
    except OSError:
        _fail("invalid_source")
    if stat.S_ISLNK(source_stat.st_mode) or not stat.S_ISDIR(resolved_stat.st_mode):
        _fail("invalid_source")
    if resolved_stat.st_uid != os.getuid():
        _fail("unsafe_source")
    return resolved


def _runtime(runtime: str) -> str:
    if runtime not in ("claude", "codex"):
        _fail("unsupported_runtime")
    return runtime


def _plain_string(value: Any) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\n" in value or "\r" in value:
        _fail("invalid_value")
    return value


def _base_model(model: str) -> str:
    model = _plain_string(model)
    if not MODEL_RE.fullmatch(model):
        _fail("invalid_model")
    return model.removesuffix("[1m]")


def _presets(runtime: str, presets: list[dict]) -> list[dict[str, str]]:
    if not isinstance(presets, list) or not presets:
        _fail("invalid_presets")
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in presets:
        if not isinstance(raw, dict) or set(raw) != {"id", "label", "model", "effort"}:
            _fail("invalid_preset")
        preset_id = _plain_string(raw["id"])
        label = _plain_string(raw["label"])
        model = _base_model(raw["model"])
        effort = raw["effort"]
        if not isinstance(effort, str) or effort not in EFFORTS or preset_id in seen:
            _fail("invalid_preset")
        seen.add(preset_id)
        normalized.append({"id": preset_id, "label": label, "model": model, "effort": effort})
    return normalized


def _safe_cache_levels(source: Path, models: set[str]) -> dict[str, list[str]]:
    cache = source / ".harness" / "codex" / "models_cache.json"
    try:
        fd=os.open(cache,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,'rb') as stream:
            cache_stat=os.fstat(stream.fileno())
            if not stat.S_ISREG(cache_stat.st_mode) or cache_stat.st_uid!=os.getuid() or cache_stat.st_nlink!=1 or cache_stat.st_mode&0o022 or cache_stat.st_size>1_000_000:
                return {}
            raw=json.loads(stream.read(1_000_001))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    entries = raw.get("models") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        return {}
    result={}
    for entry in entries:
        if not isinstance(entry,dict) or entry.get('slug') not in models or not isinstance(entry.get('supported_reasoning_levels'),list):continue
        result[entry['slug']]=list(dict.fromkeys(level['effort'] for level in entry['supported_reasoning_levels'] if isinstance(level,dict) and level.get('effort') in EFFORTS))
    return result


def _slack_approval_required(source: Path) -> bool:
    path=source/'config/launcher.env'
    try:
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,'rb') as stream:
            meta=os.fstat(stream.fileno())
            if not stat.S_ISREG(meta.st_mode) or meta.st_uid!=os.getuid() or meta.st_nlink!=1 or meta.st_mode&0o022 or meta.st_size>262144:_fail('unsafe_source_policy')
            text=stream.read(262145).decode()
    except FileNotFoundError:return False
    except (OSError,UnicodeError):_fail('unsafe_source_policy')
    declarations=[]
    for line in text.splitlines():
        if not re.match(r'^\s*(?:export\s+)?HARNESS_CODEX_SLACK_APPS\s*=',line):continue
        match=re.fullmatch(r'''\s*(?:export\s+)?HARNESS_CODEX_SLACK_APPS\s*=\s*(?:"([^"\n]*)"|'([^'\n]*)'|([A-Za-z0-9_,.-]*))\s*(?:#.*)?''',line)
        if not match:_fail('unknown_source_policy')
        value=next(value for value in match.groups() if value is not None)
        if any(char in value for char in '$`\\'):_fail('unknown_source_policy')
        declarations.append(value)
    if len(declarations)>1:_fail('unknown_source_policy')
    return bool(declarations and declarations[0])


def _capabilities(source: Path, runtime: str, presets: list[dict]) -> dict[str, Any]:
    root = _source_root(source)
    normalized = _presets(runtime, presets)
    models = list(dict.fromkeys(item["model"] for item in normalized))
    by_model = {model: [] for model in models}
    for item in normalized:
        if item["effort"] not in by_model[item["model"]]:
            by_model[item["model"]].append(item["effort"])
    if runtime == "codex":
        for model, levels in _safe_cache_levels(root, set(models)).items():
            for level in levels:
                if level not in by_model[model]:
                    by_model[model].append(level)
    result: dict[str, Any] = {
        "models": models,
        "efforts": list(dict.fromkeys(level for values in by_model.values() for level in values)),
        "efforts_by_model": by_model,
        "contexts": ["272k", "1m"] if runtime=='codex' else ["standard", "1m"],
        "permissions": [], "approvals": [], "sandboxes": [],
    }
    if runtime == "codex":
        result.update({"approvals": ['on-request'] if _slack_approval_required(root) else list(CODEX_APPROVALS), "sandboxes": list(CODEX_SANDBOXES), "networks": list(CODEX_NETWORKS)})
    else:
        result["permissions"] = list(CLAUDE_PERMISSIONS)
    return result


def capabilities(source: Path, runtime: str, presets: list[dict]) -> dict:
    """Return only supported, configured choices; never execute or source config."""
    return _capabilities(source, _runtime(runtime), presets)


def _options(options: dict | None) -> dict[str, str]:
    if options is None:
        return {}
    if not isinstance(options, dict) or set(options) - SETTING_KEYS:
        _fail("invalid_options")
    result: dict[str, str] = {}
    for key, value in options.items():
        if isinstance(value, bool) or not isinstance(value, str):
            _fail("invalid_option_type")
        result[key] = _plain_string(value)
    return result


def _session(session: dict | None, runtime: str, source: Path) -> dict[str, Any] | None:
    if session is None:
        return None
    if not isinstance(session, dict) or set(session) - SESSION_KEYS or not SESSION_KEYS <= set(session):
        _fail("invalid_session")
    native_id = session["native_id"]
    if not isinstance(native_id, str) or not UUID_RE.fullmatch(native_id):
        _fail("invalid_session")
    if session["action"] not in ("resume", "fork") or session["runtime"] != runtime:
        _fail("invalid_session")
    if session["source_root"] != str(source) or not isinstance(session["native_home"], str) or not session["native_home"].startswith("/"):
        _fail("stale_session")
    owner = session["launcher_session_id"]
    if owner is not None and (not isinstance(owner, str) or not UUID_RE.fullmatch(owner)):
        _fail("invalid_session")
    if not isinstance(session["archived"], bool):
        _fail("invalid_session")
    return dict(session, native_id=native_id.lower())


def _grant(grant: dict | None, session: dict[str, Any] | None, source: Path) -> dict[str, str] | None:
    if grant is None:
        return None
    if not isinstance(grant, dict) or set(grant) - GRANT_KEYS:
        _fail("invalid_grant")
    result: dict[str, str] = {}
    for key, value in grant.items():
        if key == "grant_provenance":
            continue
        if isinstance(value, bool) or not isinstance(value, str):
            _fail("invalid_grant")
        result[key] = _plain_string(value)
    if result.get("source_root") != str(source) or result.get("isolated") not in ("0", "1"):
        return None
    valid = {
        "permission": result.get("permission") in CLAUDE_PERMISSIONS,
        "approval": result.get("approval") in CODEX_APPROVALS,
        "sandbox": result.get("sandbox") in CODEX_SANDBOXES,
        "network": result.get("network") in CODEX_NETWORKS,
        "bypass": result.get("bypass") == "1",
        "context": result.get("context") in ("272k", "1m"),
    }
    if any(key in result and not valid[key] for key in valid):
        return None
    if session and session["launcher_session_id"] is not None:
        if result.get("isolated") != "1" or result.get("harness_session_id") != session["launcher_session_id"]:
            return None
    if session and "grant_provenance" in grant:
        provenance = grant["grant_provenance"]
        expected = {"native_id": session["native_id"], "owner": session["launcher_session_id"], "source_root": str(source), "native_home": session["native_home"]}
        if not isinstance(provenance, dict) or provenance != expected:
            return None
    return result


def _permission_selection(runtime: str, options: dict[str, str], grant: dict[str, str] | None, has_session: bool) -> tuple[dict[str, str], bool]:
    if runtime == "claude":
        if "network" in options:
            _fail("unsupported_network")
        if "approval" in options:
            _fail("unsupported_approval")
        if "sandbox" in options:
            _fail("unsupported_sandbox")
        explicit = options.get("permission")
        if explicit is not None and explicit not in CLAUDE_PERMISSIONS:
            _fail("unsupported_permission")
        selected = explicit or (grant or {}).get("permission")
        return ({"permission": selected} if selected else {}), has_session and not selected
    if "permission" in options:
        _fail("unsupported_permission")
    approval, sandbox = options.get("approval"), options.get("sandbox")
    if approval is not None and approval not in CODEX_APPROVALS:
        _fail("unsupported_approval")
    if sandbox is not None and sandbox not in CODEX_SANDBOXES:
        _fail("unsupported_sandbox")
    saved = grant or {}
    bypass = saved.get("bypass") == "1" and approval is None and sandbox is None
    if bypass:
        return ({"bypass": "1"}, False)
    selected = {"approval": approval or saved.get("approval"), "sandbox": sandbox or saved.get("sandbox")}
    selected = {key: value for key, value in selected.items() if value}
    missing = has_session and set(selected) != {"approval", "sandbox"}
    return selected, missing


def _network_selection(options: dict[str, str], grant: dict[str, str] | None, permissions: dict[str, str]) -> str:
    explicit = options.get("network")
    if explicit is not None and explicit not in CODEX_NETWORKS:
        _fail("unsupported_network")
    sandbox = "danger-full-access" if permissions.get("bypass") == "1" else permissions.get("sandbox")
    # A saved network policy only applies to the sandbox it was granted with.
    saved = (grant or {}).get("network") if sandbox == (grant or {}).get("sandbox") else None
    selected = explicit or saved
    if sandbox == "danger-full-access":
        if explicit == "disabled":
            _fail("unsupported_network_sandbox")
        return "enabled"
    if selected and not sandbox:
        _fail("explicit_sandbox_required")
    if selected and sandbox != "workspace-write":
        _fail("unsupported_network_sandbox")
    return selected or "unknown"


def _session_args(runtime: str, session: dict[str, Any] | None) -> list[str]:
    if not session:
        return []
    native_id = session["native_id"]
    if runtime == "codex":
        return ["resume" if session["action"] == "resume" else "fork", native_id]
    args = ["--resume", native_id]
    return args + (["--fork-session"] if session["action"] == "fork" else [])


def _digest(source: Path, capabilities_value: dict[str, Any], selection: dict[str, Any], grant: dict[str, str] | None) -> str:
    payload = {"capabilities": capabilities_value, "grant": grant or {}, "selection": selection, "source": str(source)}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def plan(source: Path, runtime: str, preset: str, presets: list[dict], options: dict | None = None, session: dict | None = None, grant: dict | None = None) -> dict:
    """Validate a selection and render launcher arguments without side effects."""
    runtime = _runtime(runtime)
    root = _source_root(source)
    normalized = _presets(runtime, presets)
    capability = _capabilities(root, runtime, presets)
    if not isinstance(preset, str):
        _fail("invalid_preset")
    selected_preset = next((item for item in normalized if item["id"] == preset), None)
    if selected_preset is None:
        _fail("unknown_preset")
    selected_options = _options(options)
    selected_session = _session(session, runtime, root)
    selected_grant = _grant(grant, selected_session, root)
    model = selected_options.get("model", selected_preset["model"])
    if model not in capability["models"]:
        _fail("unsupported_model")
    effort = selected_options.get("effort", selected_preset["effort"])
    if effort not in capability["efforts_by_model"][model]:
        _fail("unsupported_effort")
    native_preset=next(item for item in presets if item['id']==preset)
    default_context='272k' if runtime=='codex' else '1m' if native_preset['model'].endswith('[1m]') else 'standard'
    context = selected_options.get("context", (selected_grant or {}).get("context", default_context))
    if context not in capability["contexts"]:
        _fail("unsupported_context")
    permission_grant = None if runtime == "claude" and selected_session and selected_session["action"] == "fork" else selected_grant
    permissions, blocked = _permission_selection(runtime, selected_options, permission_grant, selected_session is not None)
    if runtime=='codex' and capability['approvals']==['on-request']:
        if selected_options.get('approval')=='never':_fail('unsupported_approval')
        if permissions.pop('bypass',None)=='1':permissions['sandbox']='danger-full-access'
        permissions['approval']='on-request'
    network = _network_selection(selected_options, selected_grant, permissions) if runtime == 'codex' else None
    blocked_reason = 'explicit_permission_required'
    if selected_session and selected_session['action'] == 'fork' and runtime == 'codex' and permissions.get('sandbox') == 'workspace-write' and network == 'unknown':
        blocked = True
        blocked_reason = 'explicit_network_required'
    summary: dict[str, Any] = {"model": model, "effort": effort, "context": context}
    summary.update(permissions)
    if summary.pop('bypass',None)=='1':summary.update(approval='never',sandbox='danger-full-access')
    if runtime == 'codex':
        summary.setdefault('sandbox', 'unknown')
        summary['network'] = network
    if selected_session:
        summary["action"] = selected_session["action"]
    selection = {"runtime": runtime, "preset": preset, "options": selected_options, "session": selected_session, "summary": summary}
    digest = _digest(root, capability, selection, selected_grant)
    if blocked:
        return {"args": [], "summary": summary, "digest": digest, "can_launch": False, "reason": blocked_reason}
    args = (["codex", preset] if runtime == "codex" else [preset])
    if runtime == "codex" and ("context" in selected_options or selected_session):
        args.append(context)
    args.append("--passthrough")
    args.extend(_session_args(runtime, selected_session))
    if "model" in selected_options or selected_session or (runtime == "claude" and "context" in selected_options):
        output_model = model + ("[1m]" if runtime == "claude" and context == "1m" else "")
        args.extend(("-m", output_model) if runtime == "codex" else ("--model", output_model))
    if "effort" in selected_options or selected_session:
        args.extend(("-c", f'model_reasoning_effort="{effort}"') if runtime == "codex" else ("--effort", effort))
    if runtime == "claude" and "permission" in permissions:
        args.extend(("--permission-mode", permissions["permission"]))
    if runtime == "codex":
        if permissions.get("bypass") == "1":
            args.append("--dangerously-bypass-approvals-and-sandbox")
        else:
            if "approval" in permissions:
                args.extend(("-a", permissions["approval"]))
            if "sandbox" in permissions:
                args.extend(("-s", permissions["sandbox"]))
        if permissions.get("sandbox") == "workspace-write" and network in CODEX_NETWORKS:
            args.extend(("-c", "sandbox_workspace_write.network_access=" + ("true" if network == "enabled" else "false")))
    return {"args": args, "summary": summary, "digest": digest, "can_launch": True}


def _main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[1] == "network-grant":
        print(network_from_overrides(argv[2:]))
        return 0
    if len(argv) != 2 or argv[1] not in ("capabilities", "plan"):
        print(json.dumps({"error": "usage"}), file=sys.stderr)
        return 2
    try:
        request = json.load(sys.stdin)
        if not isinstance(request, dict):
            _fail("invalid_request")
        source = Path(request.pop("source"))
        runtime = request.pop("runtime")
        presets = request.pop("presets")
        if argv[1] == "capabilities":
            if request:
                _fail("invalid_request")
            result = capabilities(source, runtime, presets)
        else:
            preset = request.pop("preset")
            result = plan(source, runtime, preset, presets, request.pop("options", None), request.pop("session", None), request.pop("grant", None))
            if request:
                _fail("invalid_request")
    except (PlanError, KeyError, TypeError):
        code = getattr(sys.exc_info()[1], "code", "invalid_request")
        print(json.dumps({"error": code}), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
