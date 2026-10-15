#!/usr/bin/env python3
"""One-shot, fail-closed preservation action for the trusted launcher.

The public CLI has no approval or executable override; `confirm` exists only
for in-process disposable-fixture tests.
"""
from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
from typing import Any, Callable


GIT = "/usr/bin/git"
OSASCRIPT = "/usr/bin/osascript"
ALLOWED = {"version", "operation", "repo", "expected_head", "expected_remote", "snapshot_sha256", "evidence", "expires_at", "session", "paths", "target", "stash", "ssh"}
REQUIRED = {"version", "operation", "repo", "expected_head", "expected_remote", "snapshot_sha256", "evidence", "expires_at", "session"}

_snapshot_spec = importlib.util.spec_from_file_location("preservation_snapshot", Path(__file__).with_name("preservation_snapshot.py"))
if _snapshot_spec is None or _snapshot_spec.loader is None:
    raise RuntimeError("missing trusted snapshot helper")
_snapshot_module = importlib.util.module_from_spec(_snapshot_spec)
_snapshot_spec.loader.exec_module(_snapshot_module)
capture_snapshot = _snapshot_module.capture_snapshot
_policy_spec = importlib.util.spec_from_file_location("git_policy", Path(__file__).with_name("git_policy.py"))
if _policy_spec is None or _policy_spec.loader is None: raise RuntimeError("missing git policy")
_policy = importlib.util.module_from_spec(_policy_spec); _policy_spec.loader.exec_module(_policy)


def run_git(repo: Path, *args: str, check: bool = True, lock_fd: int | None = None, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    _policy.validate(repo)
    return subprocess.run(_policy.argv(repo, *args), text=True, capture_output=True, timeout=timeout,
                          check=check, pass_fds=(() if lock_fd is None else (lock_fd,)),
                          env=_policy.ENV)


def output(repo: Path, *args: str) -> str:
    return run_git(repo, *args).stdout.strip()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _identity(path: Path) -> tuple[int, int, int, int, int, int, str]:
    info = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(info.st_mode):
        raise ValueError("unsafe fixed dependency")
    return (info.st_dev, info.st_ino, stat.S_IMODE(info.st_mode), info.st_uid,
            info.st_gid, info.st_size, sha256(path.read_bytes()))


def _dependencies() -> tuple[tuple[int, int, int, int, int, int, str], ...]:
    return tuple(_identity(path) for path in (Path(__file__), Path(__file__).with_name("preservation_snapshot.py"), Path(__file__).with_name("git_policy.py"), Path(GIT), Path(OSASCRIPT), Path("/bin/ls"), Path("/usr/bin/xattr")))


def _hex(value: Any, length: int) -> bool:
    return isinstance(value, str) and len(value) == length and all(char in "0123456789abcdef" for char in value)


def _label(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 256 and not any(ord(char) < 32 or ord(char) == 127 for char in value)


def _file_metadata(path: Path) -> dict[str, int]:
    info = path.lstat()
    if (path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & (stat.S_IRWXG | stat.S_IRWXO)):
        raise ValueError("unsafe SSH identity")
    return {key: getattr(info, f"st_{key}") for key in ("dev", "ino", "uid", "gid", "size", "mtime_ns", "ctime_ns")} | {"mode": stat.S_IMODE(info.st_mode)}


def _ancestor_metadata(path: Path) -> list[dict[str, int | str]]:
    rows: list[dict[str, int | str]] = []
    for parent in reversed((path, *path.parents)):
        info = parent.lstat(); mode = stat.S_IMODE(info.st_mode)
        if parent.is_symlink() or not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0, os.getuid()} or mode & (stat.S_IWGRP | stat.S_IWOTH) and not (info.st_uid == 0 and mode & stat.S_ISVTX):
            raise ValueError("unsafe SSH identity ancestor")
        rows.append({"path": str(parent), "dev": info.st_dev, "ino": info.st_ino, "uid": info.st_uid, "gid": info.st_gid, "mode": mode})
    return rows


def _ssh_binding(ssh: dict[str, str], repo: Path, common: Path, evidence: Any = ()) -> dict[str, Any]:
    if set(ssh) != {"hostname", "user", "identity_file"} or ssh.get("hostname") != "github.com" or ssh.get("user") != "git":
        raise ValueError("unsupported SSH binding")
    value = ssh.get("identity_file")
    if not isinstance(value, str) or "%" in value:
        raise ValueError("unsafe SSH identity")
    key = Path(value)
    if not key.is_absolute() or key.resolve(strict=True) != key or any(root == key or root in key.parents for root in (repo, common)):
        raise ValueError("unsafe SSH identity")
    metadata = _file_metadata(key)
    for row in evidence if isinstance(evidence, list) else ():
        if isinstance(row, dict) and isinstance(row.get("path"), str):
            info = Path(row["path"]).lstat()
            if (info.st_dev, info.st_ino) == (metadata["dev"], metadata["ino"]): raise ValueError("SSH identity overlaps evidence")
    return {"hostname": "github.com", "user": "git", "identity_file": str(key), "identity": metadata, "ancestors": _ancestor_metadata(key.parent)}


def _validate(request: dict[str, Any]) -> tuple[Path, list[str]]:
    if (set(request) - ALLOWED or REQUIRED - set(request) or isinstance(request.get("version"), bool)
            or not isinstance(request.get("version"), int) or request["version"] != 1):
        raise ValueError("unsupported request")
    operation = request.get("operation")
    if not isinstance(operation, str) or operation not in {"stash-paths", "ff-only", "apply-index"}:
        raise ValueError("unsupported operation")
    if not _label(request["repo"]) or not _label(request["session"]):
        raise ValueError("invalid request label")
    repo = Path(request["repo"])
    if not repo.is_absolute() or repo.is_symlink() or repo.resolve(strict=True) != repo:
        raise ValueError("repo must be absolute non-symlink")
    if "ssh" in request and (not isinstance(request["ssh"], dict) or set(request["ssh"]) != {"hostname", "user", "identity_file"}):
        raise ValueError("invalid SSH binding")
    paths = request.get("paths", [])
    if operation == "stash-paths":
        if not isinstance(paths, list) or not paths or not all(isinstance(path, str) for path in paths) or len(paths) != len(set(paths)):
            raise ValueError("paths must be nonempty and unique")
        for name in paths:
            parsed = Path(name)
            if not _label(name) or parsed.is_absolute() or ".." in parsed.parts or ".git" in parsed.parts or name.startswith("-"):
                raise ValueError("unsafe stash path")
        if "target" in request or "stash" in request:
            raise ValueError("unexpected operation field")
    elif paths:
        raise ValueError("unexpected paths")
    key = "target" if operation == "ff-only" else "stash" if operation == "apply-index" else None
    if key is not None and (set(request) & ({"target", "stash"} - {key}) or not _hex(request.get(key), 40)):
        raise ValueError(f"invalid {key}")
    if not _hex(request["expected_head"], 40) or not _hex(request["expected_remote"], 40) or not _hex(request["snapshot_sha256"], 64):
        raise ValueError("invalid snapshot binding")
    if isinstance(request["expires_at"], bool) or not isinstance(request["expires_at"], int) or request["expires_at"] <= time.time() or request["expires_at"] > time.time() + 600:
        raise ValueError("invalid expiry")
    return repo.resolve(strict=True), paths


def _evidence(request: dict[str, Any]) -> str:
    rows = request.get("evidence")
    if not isinstance(rows, list):
        raise ValueError("evidence must be a list")
    bound: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"path", "sha256"}:
            raise ValueError("invalid evidence binding")
        path, digest = row["path"], row["sha256"]
        if not isinstance(path, str):
            raise ValueError("unsafe evidence file")
        target = Path(path)
        if (not target.is_absolute() or target.is_symlink() or target.resolve(strict=True) != target
                or not target.is_file() or target.stat().st_nlink != 1):
            raise ValueError("unsafe evidence file")
        if not isinstance(digest, str) or len(digest) != 64 or sha256(target.read_bytes()) != digest:
            raise ValueError("evidence digest mismatch")
        info = target.stat()
        bound.append({"path": str(target), "sha256": digest, "dev": str(info.st_dev), "ino": str(info.st_ino), "mtime": str(info.st_mtime_ns), "uid": str(info.st_uid), "gid": str(info.st_gid), "mode": str(stat.S_IMODE(info.st_mode)), "nlink": str(info.st_nlink), "size": str(info.st_size)})
    return sha256(json.dumps(sorted(bound, key=lambda row: row["path"]), separators=(",", ":"), sort_keys=True).encode())


def _lock(common: Path) -> int:
    lock = common / "harness-deliver.lock"
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    info = os.fstat(fd)
    named = lock.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & (stat.S_IWGRP | stat.S_IWOTH)
            or (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino)):
        os.close(fd)
        raise ValueError("unsafe delivery lock")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise ValueError("delivery lock held")
    if os.fstat(fd).st_ino != lock.lstat().st_ino:
        os.close(fd)
        raise ValueError("unsafe delivery lock")
    return fd


def _lock_matches(fd: int, common: Path) -> bool:
    held, named = os.fstat(fd), (common / "harness-deliver.lock").lstat()
    return (stat.S_ISREG(held.st_mode) and held.st_nlink == 1 and held.st_uid == os.getuid()
            and not stat.S_IMODE(held.st_mode) & (stat.S_IWGRP | stat.S_IWOTH)
            and (held.st_dev, held.st_ino) == (named.st_dev, named.st_ino))


def native_confirm(prompt: str, seconds: int) -> bool:
    script = 'on run argv\n set answer to display dialog (item 1 of argv) buttons {"Cancel", "Run once"} default button "Cancel" cancel button "Cancel" with icon caution giving up after (item 2 of argv as integer)\n if gave up of answer then return "gave up"\n return button returned of answer\nend run'
    try:
        result = subprocess.run([OSASCRIPT, "-e", script, prompt, str(seconds)], text=True, capture_output=True,
                                timeout=seconds + 5, env={"PATH": "/usr/bin:/bin", "LC_ALL": "C", "LANG": "C"})
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.strip() == "Run once"


def _confirm(confirm: Callable[[str], bool], prompt: str, seconds: int) -> bool:
    if confirm is native_confirm:
        return native_confirm(prompt, max(1, seconds))
    result = confirm(prompt)
    if result is True or result is False:
        return result
    raise ValueError("malformed confirmation result")


def _prompt(repo: Path, request: dict[str, Any], paths: list[str], evidence_digest: str, ssh_binding: dict[str, Any] | None) -> str:
    argv = _policy.operation_argv(repo, request["operation"], tuple(paths), request.get("target", ""), request.get("stash", ""))
    evidence = json.dumps(request["evidence"], sort_keys=True, separators=(",", ":"))
    argv_json = json.dumps(argv, separators=(",", ":"))
    prompt = (f"Run once\nargv={argv_json}\nrepo={repo}\nsession={request['session']}\n"
              f"origin={_policy.validate(repo)[2]}\nsnapshot={request['snapshot_sha256']}\nevidence={evidence}\nevidence-digest={evidence_digest}\nexpires={request['expires_at']}\nNo automatic undo.")
    if ssh_binding is not None: prompt += f"\nssh-binding={json.dumps(ssh_binding, sort_keys=True, separators=(',', ':'))}"
    if len(prompt) > 4096: raise ValueError("prompt too large")
    return prompt


def _live_remote(repo: Path, deadline: float | None = None, ssh: dict[str, str] | None = None) -> str:
    timeout = None if deadline is None else max(0.01, deadline - time.monotonic())
    url = _policy.validate(repo, timeout=timeout)[2]
    timeout = None if deadline is None else max(0.01, deadline - time.monotonic())
    result = subprocess.run(_policy.remote_argv(repo, url, ssh), text=True, capture_output=True, timeout=timeout, env=_policy.ENV)
    if result.returncode: raise ValueError("live remote unavailable")
    remote = result.stdout.split()
    if len(remote) < 2:
        raise ValueError("live remote unavailable")
    return remote[0]


def _valid_stash_paths(repo: Path, paths: list[str]) -> bool:
    dirty = set(run_git(repo, "diff", "--name-only", "-z", "--", *paths).stdout.split("\0"))
    for path in paths:
        if path not in dirty:
            return False
        if run_git(repo, "ls-files", "--error-unmatch", "--", path, check=False).returncode:
            return False
    return True


def _valid_stash_result(repo: Path, stash: str, head: str, paths: list[str]) -> bool:
    parents = output(repo, "rev-list", "--parents", "-n", "1", stash).split()
    if len(parents) != 3 or parents[1] != head:
        return False
    if output(repo, "rev-parse", f"{stash}^2^{{tree}}") != output(repo, "rev-parse", f"{head}^{{tree}}"):
        return False
    changed = set(run_git(repo, "diff", "--name-only", "-z", head, stash).stdout.split("\0")) - {""}
    return changed == set(paths)


def _operation(repo: Path, request: dict[str, Any], paths: list[str], lock_fd: int, deadline: float, ssh_binding: dict[str, Any] | None) -> tuple[str, str]:
    if time.time() >= request["expires_at"] or time.monotonic() >= deadline:
        return "rejected-drift", ""
    operation = request["operation"]
    if operation == "stash-paths":
        old = run_git(repo, "rev-parse", "--verify", "--quiet", "refs/stash", check=False).stdout.strip()
        result = _run_mutation(repo, request, paths, lock_fd, deadline, ssh_binding)
        if result is None: return "rejected-drift", ""
        new = run_git(repo, "rev-parse", "--verify", "--quiet", "refs/stash", check=False).stdout.strip()
        return ("success", new) if result.returncode == 0 and new and new != old and _valid_stash_result(repo, new, request["expected_head"], paths) else ("failed-or-partial", "")
    if operation == "ff-only":
        target = request["target"]
        if target != request["expected_remote"] or run_git(repo, "merge-base", "--is-ancestor", request["expected_head"], target, check=False).returncode != 0:
            return "rejected", ""
        result = _run_mutation(repo, request, paths, lock_fd, deadline, ssh_binding)
        if result is None: return "rejected-drift", ""
        return ("success", target) if result.returncode == 0 and output(repo, "rev-parse", "HEAD") == target else ("failed-or-partial", "")
    stash = request["stash"]
    if output(repo, "rev-parse", "refs/stash") != stash or len(output(repo, "rev-list", "--parents", "-n", "1", stash).split()) < 3:
        return "rejected", ""
    result = _run_mutation(repo, request, paths, lock_fd, deadline, ssh_binding)
    if result is None: return "rejected-drift", ""
    return ("success", stash) if result.returncode == 0 and output(repo, "rev-parse", "refs/stash") == stash else ("failed-or-partial", "")


def _run_mutation(repo: Path, request: dict[str, Any], paths: list[str], lock_fd: int, deadline: float, ssh_binding: dict[str, Any] | None) -> subprocess.CompletedProcess[str] | None:
    remaining = deadline - time.monotonic()
    if remaining <= 0: return None
    try:
        _policy.validate(repo, timeout=remaining)
        if ssh_binding is not None:
            common = Path(output(repo, "rev-parse", "--git-common-dir"))
            common = (common if common.is_absolute() else repo / common).resolve(strict=True)
            try:
                if _ssh_binding(request["ssh"], repo, common, request["evidence"]) != ssh_binding: return None
            except (OSError, ValueError):
                return None
    except subprocess.TimeoutExpired:
        return None
    if time.time() >= request["expires_at"] or time.monotonic() >= deadline:
        return None
    argv = _policy.operation_argv(repo, request["operation"], tuple(paths), request.get("target", ""), request.get("stash", ""))
    return subprocess.run(argv, text=True, capture_output=True, check=False, pass_fds=(lock_fd,), env=_policy.ENV)


def execute_request(request: dict[str, Any], confirm: Callable[[str], bool] = native_confirm) -> dict[str, str]:
    """Execute one stash only after full before/after confirmation snapshot equality."""
    try:
        repo, paths = _validate(request)
        deadline = time.monotonic() + (request["expires_at"] - time.time())
        common = Path(output(repo, "rev-parse", "--git-common-dir"))
        fd = _lock((common if common.is_absolute() else repo / common).resolve(strict=True))
        try:
            dependencies_before = (_dependencies(), _policy.dependencies())
            config_before = _policy.validate(repo)
            ssh_binding = None if "ssh" not in request else _ssh_binding(request["ssh"], repo, (common if common.is_absolute() else repo / common).resolve(strict=True), request["evidence"])
            evidence_before = _evidence(request)
            before = capture_snapshot(repo)
            if before["sha256"] != request["snapshot_sha256"] or before["head"] != request["expected_head"] or before["remote"] != request["expected_remote"]:
                return {"status": "rejected-drift"}
            if _live_remote(repo, deadline, request.get("ssh")) != request["expected_remote"]:
                return {"status": "rejected-drift"}
            if request["operation"] == "ff-only" and (output(repo, "symbolic-ref", "--short", "HEAD") != "main" or output(repo, "rev-parse", "--abbrev-ref", "@{upstream}") != "origin/main"):
                return {"status": "rejected"}
            if run_git(repo, "diff", "--cached", "--quiet", check=False).returncode != 0 or run_git(repo, "ls-files", "--unmerged", "-z").stdout:
                return {"status": "rejected-index"}
            if request["operation"] == "stash-paths" and not _valid_stash_paths(repo, paths):
                return {"status": "rejected"}
            if time.monotonic() >= deadline or not _confirm(confirm, _prompt(repo, request, paths, evidence_before, ssh_binding), int(deadline - time.monotonic())):
                return {"status": "cancelled"}
            try:
                ssh_after = None if ssh_binding is None else _ssh_binding(request["ssh"], repo, (common if common.is_absolute() else repo / common).resolve(strict=True), request["evidence"])
            except (OSError, ValueError):
                return {"status": "rejected-drift"}
            if ssh_after != ssh_binding:
                return {"status": "rejected-drift"}
            after = capture_snapshot(repo)
            try:
                evidence_after = _evidence(request)
            except ValueError:
                return {"status": "rejected-drift"}
            if (time.monotonic() >= deadline or not _lock_matches(fd, common if common.is_absolute() else repo / common) or after["sha256"] != before["sha256"]
                    or evidence_after != evidence_before or (_dependencies(), _policy.dependencies()) != dependencies_before or _policy.validate(repo) != config_before
                    or ssh_after != ssh_binding):
                return {"status": "rejected-drift"}
            if _live_remote(repo, deadline, request.get("ssh")) != request["expected_remote"] or time.monotonic() >= deadline:
                return {"status": "rejected-drift"}
            try:
                status, _detail = _operation(repo, request, paths, fd, deadline, ssh_binding)
            except (OSError, subprocess.SubprocessError, ValueError):
                return {"status": "failed-or-partial"}
            return {"status": status}
        finally:
            os.close(fd)
    except (OSError, ValueError, subprocess.SubprocessError):
        return {"status": "rejected"}


def _strict_load(data: bytes) -> dict[str, Any]:
    def reject_duplicates(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    if len(data) > 65536:
        raise ValueError("request too large")
    parsed = json.loads(data.decode("utf-8"), object_pairs_hook=reject_duplicates)
    if not isinstance(parsed, dict):
        raise ValueError("request object required")
    return parsed


def main() -> int:
    try:
        request = _strict_load(sys.stdin.buffer.read())
        _validate(request)
        print(json.dumps(execute_request(request), sort_keys=True))
        return 0
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        print(json.dumps({"status": "rejected"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
