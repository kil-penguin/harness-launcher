"""Read-only metadata snapshot for a strict ordinary Git repository."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
from typing import Any
import importlib.util

GIT = "/usr/bin/git"
ENV = {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "LANG": "C", "GIT_OPTIONAL_LOCKS": "0"}
_policy_spec = importlib.util.spec_from_file_location("git_policy", Path(__file__).with_name("git_policy.py"))
if _policy_spec is None or _policy_spec.loader is None: raise RuntimeError("missing git policy")
_policy = importlib.util.module_from_spec(_policy_spec); _policy_spec.loader.exec_module(_policy)


def _git(repo: Path, *args: str) -> bytes:
    _policy.validate(repo)
    result = subprocess.run(_policy.argv(repo, *args), env=_policy.ENV, capture_output=True)
    if result.returncode: raise ValueError("git snapshot command refused")
    return result.stdout


def _meta(path: Path) -> dict[str, Any]:
    if not path.exists() and not path.is_symlink(): return {"type": "absent"}
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)): raise ValueError("unsafe snapshot node")
    if stat.S_ISREG(info.st_mode) and info.st_nlink != 1: raise ValueError("hardlinked snapshot file")
    acl = subprocess.run(["/bin/ls", "-lde", str(path)], env=ENV, capture_output=True, check=True).stdout.decode().splitlines()
    names = subprocess.run(["/usr/bin/xattr", "-s", str(path)], env=ENV, capture_output=True, check=True).stdout.decode().splitlines()
    xattrs = {name: hashlib.sha256(subprocess.run(["/usr/bin/xattr", "-p", "-x", "-s", name, str(path)], env=ENV, capture_output=True, check=True).stdout).hexdigest() for name in sorted(names)}
    value = {"type": "file" if stat.S_ISREG(info.st_mode) else "directory", "mode": stat.S_IMODE(info.st_mode),
             "uid": info.st_uid, "gid": info.st_gid, "nlink": info.st_nlink, "mtime_ns": info.st_mtime_ns,
             "flags": getattr(info, "st_flags", 0), "acl": [row.strip() for row in acl if row.lstrip()[:1].isdigit() and ":" in row], "xattrs_sha256": xattrs}
    if stat.S_ISREG(info.st_mode): value.update(size=info.st_size, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    return value


def capture_snapshot(repo: Path) -> dict[str, Any]:
    repo = repo.resolve(strict=True)
    if repo.is_symlink() or not (repo / ".git").is_dir() or _git(repo, "branch", "--show-current").strip() != b"main": raise ValueError("ordinary main repository required")
    status = _git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=normal")
    if _git(repo, "diff", "--cached", "--quiet") or _git(repo, "ls-files", "--unmerged", "-z"): raise ValueError("staged or unmerged state")
    flags = _git(repo, "ls-files", "-v", "-z")
    if any(row[:1] in b"sS" or row[:1].islower() for row in flags.split(b"\0") if row): raise ValueError("hidden index flags")
    paths = sorted(set(os.fsdecode(value) for value in _git(repo, "ls-files", "--modified", "--deleted", "--others", "--exclude-standard", "-z").split(b"\0") if value))
    nodes: dict[str, Any] = {}
    parents: set[str] = set()
    for name in paths:
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or ".git" in relative.parts: raise ValueError("unsafe pending path")
        for parent in relative.parents:
            if parent != Path("."):
                if (repo / parent).is_symlink(): raise ValueError("symlink ancestor")
                parents.add(str(parent))
        nodes[name] = _meta(repo / relative)
    for name in parents: nodes[name] = _meta(repo / name)
    common = Path(os.fsdecode(_git(repo, "rev-parse", "--git-common-dir")).strip())
    if not common.is_absolute(): common = repo / common
    index = Path(os.fsdecode(_git(repo, "rev-parse", "--git-path", "index")).strip())
    if not index.is_absolute(): index = repo / index
    if index.with_name("index.lock").exists(): raise ValueError("index lock exists")
    index_nodes = {item.name: _meta(item) for item in [index, *sorted(index.parent.glob("sharedindex.*"))]}
    data = {"head": _git(repo, "rev-parse", "HEAD").decode().strip(), "remote": _git(repo, "rev-parse", "refs/remotes/origin/main").decode().strip(), "status": status.hex(), "nodes": dict(sorted(nodes.items())), "index": _meta(index), "index_bytes": hashlib.sha256(index.read_bytes()).hexdigest(), "stage": _git(repo, "ls-files", "--stage", "-z").hex(), "flags": flags.hex(), "refs": _git(repo, "show-ref").hex(), "common": str(common.resolve()), "index_nodes": index_nodes, "parents": sorted(parents), "repoidentity": _meta(repo)}
    data["sha256"] = hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return data
