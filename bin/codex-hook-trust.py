#!/usr/bin/env python3
"""Carry exact approved source hooks into a broker-bound isolated home.

This never approves a new hook. Missing or changed evidence leaves native
Codex review intact. The caller publishes the returned config under its CAS.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import tomllib
import uuid

EVENTS = {
    "SessionStart": "session_start", "UserPromptSubmit": "user_prompt_submit",
    "PreToolUse": "pre_tool_use", "PostToolUse": "post_tool_use",
    "PermissionRequest": "permission_request", "Stop": "stop",
    "SubagentStart": "subagent_start", "SubagentStop": "subagent_stop",
    "PreCompact": "pre_compact", "PostCompact": "post_compact",
}
PROFILE_FILES = {f"{name}.config.toml" for name in ("fast", "base", "sol", "astra", "plan", "rich")}
PACKAGE_CALLBACKS = {"codex-hook-adapter.sh", "codex-pretool-adapter.py", "codex-cmux-title-sync.py", "harness-launch-record"}


def split_profile_state(text):
    """Separate native hook decisions without ignoring other profile content."""
    tomllib.loads(text)
    managed, state = [], []
    in_state = False
    for line in text.splitlines(keepends=True):
        if re.match(r"^\s*\[.*\]\s*(?:#.*)?$", line):
            in_state = bool(re.match(r"^\s*\[hooks\.state(?:\.|\])", line))
        (state if in_state else managed).append(line)
    return "".join(managed).rstrip() + "\n", "".join(state)


def profile_signature(path):
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return "invalid-profile:unreadable"
    try:
        managed, _ = split_profile_state(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return "invalid-profile:" + hashlib.sha256(raw).hexdigest()
    return "sha256:" + hashlib.sha256(managed.encode()).hexdigest()


def preserve_profile(candidate, live):
    managed, _ = split_profile_state(candidate)
    try:
        if isinstance(live, bytes):
            live = live.decode("utf-8")
        _, state = split_profile_state(live)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        state = ""
    result = managed + ("\n" + state if state else "")
    tomllib.loads(result)
    return result


def fingerprint(event, matcher, handler):
    """Command subset verified against native 0.161 hooks/list currentHash."""
    if event not in EVENTS or handler.get("type") != "command":
        raise ValueError("unsupported hook")
    if set(handler) - {"type", "command", "timeout", "async", "statusMessage"}:
        raise ValueError("unsupported hook fields")
    timeout = handler.get("timeout", 600)
    if type(timeout) is not int or type(handler.get("async", False)) is not bool:
        raise ValueError("unsupported hook values")
    if not isinstance(handler.get("command"), str):
        raise ValueError("missing command")
    normalized = dict(type="command", command=handler["command"],
                      timeout=max(1, timeout), **{"async": handler.get("async", False)})
    if handler.get("statusMessage") is not None:
        normalized["statusMessage"] = handler["statusMessage"]
    identity = {"event_name": EVENTS[event], "hooks": [normalized]}
    if matcher is not None and event not in ("UserPromptSubmit", "Stop"):
        identity["matcher"] = matcher
    payload = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


def regular(path, boundary=None, limit=2 * 1024 * 1024, private=False):
    """Bounded fd reads; broker/source paths are walked without links."""
    path = Path(path)
    directory = None
    try:
        if boundary is not None:
            parts = path.relative_to(boundary).parts
            directory = os.open(boundary, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            for component in parts[:-1]:
                info = os.fstat(directory)
                if info.st_uid != os.getuid() or info.st_mode & 0o022:
                    raise ValueError("unsafe evidence directory")
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                os.close(directory)
                directory = child
            info = os.fstat(directory)
            if info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise ValueError("unsafe evidence directory")
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        else:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                    info.st_nlink != 1 or info.st_mode & 0o022 or info.st_size > limit or
                    (private and stat.S_IMODE(info.st_mode) != 0o600)):
                raise ValueError("unsafe evidence file")
            raw = stream.read(limit + 1)
            if len(raw) > limit:
                raise ValueError("evidence grew")
            return raw
    finally:
        if directory is not None:
            os.close(directory)


def git_output(target, *args):
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null")
    return subprocess.run(
        ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
         "-C", str(target), *args], env=env, capture_output=True, timeout=5, check=True).stdout


def root_pattern(source):
    return re.compile(r"(?<![A-Za-z0-9_./-])" + re.escape(str(source)) +
                      r"(/core/hooks/[A-Za-z0-9_.-]+\.sh)(?=$|[\s\"'])")


def approved_package_command(command, grant):
    """Normalize one generator-owned callback token after both byte proofs."""
    tokens = shlex.split(command)
    references = [(index, token) for index, token in enumerate(tokens) if Path(token).name in PACKAGE_CALLBACKS]
    if not references:
        return command
    if len(references) != 1 or references[0][0] != 1 or command != shlex.join(tokens):
        raise ValueError("unsupported callback argv")
    _, token = references[0]
    path = Path(token)
    name = path.name
    shapes = {"codex-hook-adapter.sh": (len(tokens) == 4 and tokens[0] == "bash"),
              "codex-pretool-adapter.py": len(tokens) == 3,
              "codex-cmux-title-sync.py": len(tokens) == 2,
              "harness-launch-record": (len(tokens) == 3 and tokens[2] == "codex")}
    if not shapes[name] or path not in {Path(__file__).parent / name, Path(__file__).resolve().parent / name}:
        raise ValueError("callback outside current verified package")
    approved = [p for p in grant["callbacks"] if Path(p).name == name]
    if len(approved) != 1:
        raise ValueError("ambiguous callback approval")
    old = approved[0]
    expected = grant["callbacks"][old]
    if hashlib.sha256(regular(path)).hexdigest() != expected or hashlib.sha256(regular(old)).hexdigest() != expected:
        raise ValueError("callback bytes changed")
    tokens[1] = old
    return shlex.join(tokens)


def identity_stat(path):
    return os.lstat(path)


def root_identity_matches(recorded, path):
    """Recorded "<dev> <ino>" names `path` (a real directory, never followed).

    Same inode and either the same device, or a whole-volume device renumbering
    (reboot) where the root still sits on the device of its worktrees directory.
    A root replaced by another filesystem has a different device from its parent.
    """
    fields = recorded.split()
    if len(fields) != 2 or not all(re.fullmatch(r"[0-9]+", field) for field in fields):
        return False
    device, inode = int(fields[0]), int(fields[1])
    try:
        path = Path(path)
        info = identity_stat(path)
        if not stat.S_ISDIR(info.st_mode) or info.st_ino != inode:
            return False
        return info.st_dev == device or identity_stat(path.parent).st_dev == info.st_dev
    except OSError:
        return False


def broker_source(published):
    target = published.parent.parent
    sid = os.environ.get("HARNESS_SESSION_ID", "")
    uuid.UUID(sid)
    state = Path(os.environ.get("HARNESS_SESSION_STATE_HOME") or
                 Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "harness-launcher")
    state = state.resolve()
    record = state / "sessions" / sid
    if target != state / "worktrees" / sid or target.resolve() != target:
        raise ValueError("not a physical broker root")
    if record.is_symlink() or record.stat().st_uid != os.getuid():
        raise ValueError("foreign broker record")
    source = Path(regular(record / "source-root", state).decode().strip())
    if source.resolve() != source or source == target:
        raise ValueError("invalid source root")
    expected = regular(record / "session-root", state).decode().strip()
    if expected != str(target) or os.environ.get("HARNESS_SESSION_ROOT") != str(target):
        raise ValueError("session association changed")
    if os.environ.get("HARNESS_SOURCE_ROOT") != str(source):
        raise ValueError("source association changed")
    if not root_identity_matches(regular(record / "root-inode", state).decode(), target):
        raise ValueError("session root replaced")
    if [line for line in regular(record / "journal", state).decode().splitlines() if line.startswith("state=")] != ["state=OPEN"]:
        raise ValueError("session is not open")
    base = regular(record / "base-sha", state).decode().strip()
    if not re.fullmatch(r"[0-9a-f]{40}", base):
        raise ValueError("invalid snapshot")
    return source, target, base, state


def equivalent_command(command, desired, source, target, base, grant):
    """Only literal core/hooks script arguments may change roots."""
    pattern = root_pattern(source)
    paths = [match.group(1).lstrip("/") for match in pattern.finditer(command)]
    normalized = root_pattern(target).sub(lambda match: str(source) + match.group(1),
                                         approved_package_command(desired, grant))
    if command != normalized:
        return False
    tokens = shlex.split(command)
    if paths:
        # The generator emits a direct bash hook or one of two adapters.
        valid = ((len(tokens) == 2 and tokens[0] == "bash") or
                 (len(tokens) == 4 and tokens[0] == "bash" and
                  Path(tokens[1]).name == "codex-hook-adapter.sh") or
                 (len(tokens) == 3 and Path(tokens[1]).name == "codex-pretool-adapter.py"))
        if not valid:
            return False
        if len(tokens) > 2 and not callback_approved(Path(tokens[1]), grant):
            return False
        for relative in paths:
            original = regular(source / relative, source)
            isolated = regular(target / relative, target)
            approved_script = grant["scripts"].get(relative, {})
            approved_sha = approved_script.get("sha256")
            if hashlib.sha256(isolated).hexdigest() != approved_sha:
                return False
            approved_blob = git_output(target, "show", f"{grant['approved_snapshot']['base_sha']}:{relative}")
            if hashlib.sha256(approved_blob).hexdigest() != approved_sha or approved_script.get("base_blob_sha256") != approved_sha:
                return False
            blob = git_output(target, "show", f"{base}:{relative}")
            if isolated != blob:
                return False
        return True
    # Existing status-only registry commands stay literal, with no root rewrite.
    for script, argument in (("$HOME/.orca/agent-hooks/codex-hook.sh", ""),
                             ("$HOME/.codex/herdr-agent-state.sh", " session")):
        expected = (f"/bin/sh -c 's=\"{script}\"; [ -x \"$s\" ] && {{ /bin/sh \"$s\"{argument} "
                    ">/dev/null 2>&1; exit 0; }; cat >/dev/null'")
        if command == expected:
            return callback_approved(Path(script.replace("$HOME", str(Path.home()))), grant)
    return (len(tokens) in (2, 3) and callback_approved(Path(tokens[1]), grant) and
            Path(tokens[1]).name in {"codex-cmux-title-sync.py", "harness-launch-record"} and
            (len(tokens) == 2 or tokens[2] == "codex"))


def callback_approved(path, grant):
    expected = grant["callbacks"].get(str(path))
    return bool(expected and hashlib.sha256(regular(path)).hexdigest() == expected)


def normalized_hooks(hooks, target, source, grant):
    result = json.loads(json.dumps(hooks))
    for groups in result.get("hooks", {}).values():
        for group in groups:
            for handler in group.get("hooks", []):
                if isinstance(handler.get("command"), str):
                    handler["command"] = root_pattern(target).sub(lambda match: str(source) + match.group(1),
                                                                  approved_package_command(handler["command"], grant))
    return result


def approval_record(state, source, target, hooks, base):
    path = state / "hook-approvals" / (hashlib.sha256(str(source).encode()).hexdigest() + ".json")
    grant = json.loads(regular(path, state, private=True))
    if grant.get("schema_version") != 1 or grant.get("source") != str(source):
        raise ValueError("foreign approval")
    if grant.get("algorithm") != "codex-0.161-command-hooks" or not re.fullmatch(r"[0-9a-f]{64}", grant.get("approval_receipt_sha256", "")):
        raise ValueError("missing approval provenance")
    snapshot = grant.get("approved_snapshot", {})
    if not re.fullmatch(r"[0-9a-f]{40}", snapshot.get("base_sha", "")) or not re.fullmatch(r"[0-9a-f]{64}", snapshot.get("audit_raw_hooks_sha256", "")):
        raise ValueError("missing approved snapshot")
    for commit in (base, snapshot["base_sha"]):
        if git_output(target, "cat-file", "-t", commit).strip() != b"commit":
            raise ValueError("missing source snapshot commit")
    normalized = normalized_hooks(hooks, target, source, grant)
    normalized_sha = hashlib.sha256(json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    if (grant.get("normalization") != "literal-core-and-approved-callback-paths-v1" or
            grant.get("normalized_hooks_sha256") != normalized_sha or grant.get("hooks") != normalized):
        raise ValueError("approved hook set changed")
    native = Path(os.environ.get("HARNESS_CODEX_BIN") or shutil.which("codex") or "")
    if hashlib.sha256(regular(native.resolve(), limit=256 * 1024 * 1024)).hexdigest() != grant.get("codex_binary_sha256"):
        raise ValueError("native executable changed")
    version = subprocess.run([str(native), "--version"], text=True, capture_output=True, timeout=5, check=True).stdout.strip()
    if version != grant.get("codex_version"):
        raise ValueError("native version changed")
    return grant


def inherit(config, candidate_hooks, published):
    """Append verified entries only; existing target decisions always win."""
    try:
        source, target, base, state = broker_source(published)
        source_home = source / ".harness/codex"
        old_hooks = json.loads(regular(source_home / "hooks.json", source))
        new_hooks = json.loads(regular(candidate_hooks))
        grant = approval_record(state, source, target, new_hooks, base)
        old_state = tomllib.loads(regular(source_home / "config.toml", source).decode()).get("hooks", {}).get("state", {})
        live_state = tomllib.loads(config).get("hooks", {}).get("state", {})
    except (OSError, ValueError, UnicodeError, KeyError, subprocess.SubprocessError):
        return config
    entries = []
    for event, groups in new_hooks.get("hooks", {}).items():
        for gi, group in enumerate(groups):
            for hi, handler in enumerate(group.get("hooks", [])):
                try:
                    if event not in EVENTS or set(group) - {"hooks", "matcher"}:
                        continue
                    key = f"{published / 'hooks.json'}:{EVENTS[event]}:{gi}:{hi}"
                    old_key = f"{source_home / 'hooks.json'}:{EVENTS[event]}:{gi}:{hi}"
                    approved = old_state.get(old_key, {})
                    old_group = old_hooks["hooks"][event][gi]
                    old_handler = old_group["hooks"][hi]
                    if key in live_state or old_group.get("matcher") != group.get("matcher"):
                        continue
                    if approved.get("trusted_hash") != fingerprint(event, old_group.get("matcher"), old_handler):
                        continue
                    if {k: v for k, v in old_handler.items() if k != "command"} != {k: v for k, v in handler.items() if k != "command"}:
                        continue
                    if not equivalent_command(old_handler["command"], handler["command"], source, target, base, grant):
                        continue
                    fields = f"\n[hooks.state.{json.dumps(key)}]\ntrusted_hash = {json.dumps(fingerprint(event, group.get('matcher'), handler))}\n"
                    if "enabled" in approved:
                        if type(approved["enabled"]) is not bool:
                            continue
                        fields += "enabled = " + str(approved["enabled"]).lower() + "\n"
                    entries.append(fields)
                except (OSError, ValueError, UnicodeError, KeyError, IndexError, subprocess.SubprocessError):
                    continue
    result = config.rstrip() + "\n" + "".join(entries) if entries else config
    tomllib.loads(result)
    return result
