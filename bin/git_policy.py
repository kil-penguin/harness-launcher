"""Fixed Git configuration policy for the trusted transaction candidate."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess

GIT = "/usr/bin/git"
SSH = "/usr/bin/ssh"
UPLOAD_PACK = "/usr/bin/git-upload-pack"
ENV = {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "LANG": "C", "GIT_OPTIONAL_LOCKS": "0",
       "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_SYSTEM": "/dev/null", "GIT_CONFIG_GLOBAL": "/dev/null"}
PREFIX = ("-c", "core.fsmonitor=false", "-c", "core.useBuiltinFSMonitor=false", "-c", "diff.external=false")
DENIED = ("include.", "includeif.", "core.fsmonitor", "core.sshcommand", "diff.external", "credential.", "url.")
HOOKS = ("post-checkout", "post-merge", "reference-transaction", "post-index-change", "pre-push")
SCP_GITHUB = re.compile(r"git@[A-Za-z0-9][A-Za-z0-9.-]*:[A-Za-z0-9_][A-Za-z0-9_.-]*/[A-Za-z0-9_][A-Za-z0-9_.-]*\.git\Z")


def identity(path: Path, single_link: bool = True) -> tuple[int, int, int, int, int, int, str]:
    info = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(info.st_mode) or single_link and info.st_nlink != 1:
        raise ValueError("unsafe git policy input")
    return info.st_dev, info.st_ino, info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode), info.st_size, hashlib.sha256(path.read_bytes()).hexdigest()


def _config(repo: Path) -> Path:
    path = repo / ".git" / "config"
    if path.resolve(strict=True) != repo.resolve(strict=True) / ".git" / "config":
        raise ValueError("unsafe git config")
    return path


def _origin(rows: tuple[str, ...], repo: Path | None = None, timeout: float | None = None) -> str:
    values = [row.split("\n", 1)[1] for row in rows if row.startswith("remote.origin.url\n")]
    if len(values) != 1 or not values[0] or any(char.isspace() for char in values[0]): raise ValueError("unsupported origin")
    url = values[0]
    if url.startswith(("-", "ext::", "file:")) or "::" in url: raise ValueError("unsupported origin")
    if url.startswith("/"):
        if repo is None: raise ValueError("unsupported origin")
        target = Path(url)
        if target.resolve(strict=True) != target or not target.is_dir() or not (target / "HEAD").is_file(): raise ValueError("unsupported origin")
        checked = subprocess.run([GIT, "--git-dir", str(target), "rev-parse", "--is-bare-repository"], env=ENV, text=True, capture_output=True, timeout=timeout)
        if checked.returncode or checked.stdout.strip() != "true": raise ValueError("unsupported origin")
        return url
    if url.startswith("https://") or url.startswith("ssh://"):
        return url
    if url.count(":") == 1 and "@" in url.split(":", 1)[0] and url.split(":", 1)[1] and not url.startswith("-"):
        return url
    raise ValueError("unsupported origin")


def _hooks(repo: Path, rows: tuple[str, ...] = ()) -> tuple[tuple[str, tuple[int, int, int, int, str]], ...]:
    configured = [row.split("\n", 1)[1] for row in rows if row.startswith("core.hookspath\n")]
    if len(configured) > 1: raise ValueError("unsafe hooks")
    value = configured[0] if configured else ".git/hooks"
    folder = Path(value) if Path(value).is_absolute() else repo / value
    if folder.resolve(strict=True) != folder or not folder.is_dir(): raise ValueError("unsafe hooks")
    found = []
    for name in HOOKS:
        path = folder / name
        if path.exists() or path.is_symlink():
            if path.is_symlink() or os.access(path, os.X_OK): raise ValueError("unreviewed executable hook")
            found.append((name, identity(path)))
    return tuple(found)


def validate(repo: Path, timeout: float | None = None) -> tuple[tuple[int, int, int, int, str], tuple[str, ...], str, tuple[tuple[str, tuple[int, int, int, int, str]], ...]]:
    path = _config(repo)
    config_identity = identity(path)
    result = subprocess.run([GIT, "config", "--no-includes", "--null", "--file", str(path), "--list"], env=ENV, capture_output=True, timeout=timeout)
    if result.returncode:
        raise ValueError("invalid git config")
    rows = tuple(os.fsdecode(row) for row in result.stdout.split(b"\0") if row)
    keys = tuple(row.split("\n", 1)[0].lower() for row in rows)
    if any(key.startswith(DENIED) or key.startswith("filter.") or key.startswith("merge.") and key.endswith((".driver", ".recursive")) or key.startswith("remote.") and key.endswith((".uploadpack", ".receivepack")) for key in keys):
        raise ValueError("unsafe git config")
    return config_identity, rows, _origin(rows, repo, timeout), _hooks(repo, rows)


def _ssh_command(ssh: dict[str, str] | None = None) -> str:
    parts = [SSH, "-F", "/dev/null", "-oBatchMode=yes", "-oPermitLocalCommand=no"]
    if ssh is not None:
        if (set(ssh) != {"hostname", "user", "identity_file"} or ssh.get("hostname") != "github.com"
                or ssh.get("user") != "git" or not isinstance(ssh.get("identity_file"), str)
                or not Path(ssh["identity_file"]).is_absolute()):
            raise ValueError("unsupported SSH binding")
        parts.extend(["-oIdentitiesOnly=yes", "-oIdentityAgent=none", "-oCertificateFile=none", "-oHostName=github.com", "-oUser=git", "-i", ssh["identity_file"]])
    return shlex.join(parts)


def argv(repo: Path, *args: str, ssh: dict[str, str] | None = None) -> list[str]:
    ssh_command = _ssh_command(ssh)
    return [GIT, *PREFIX, "-c", f"core.sshCommand={ssh_command}", "-C", str(repo), *args]


def remote_argv(repo: Path, url: str, ssh: dict[str, str] | None = None) -> list[str]:
    if ssh is not None and SCP_GITHUB.fullmatch(url) is None:
        raise ValueError("unsupported SSH origin")
    if url.startswith("/"):
        return argv(repo, "ls-remote", "--exit-code", f"--upload-pack={UPLOAD_PACK}", url, "refs/heads/main", ssh=ssh)
    return argv(repo, "ls-remote", "--exit-code", url, "refs/heads/main", ssh=ssh)


def operation_argv(repo: Path, operation: str, paths: tuple[str, ...] = (), target: str = "", stash: str = "") -> list[str]:
    if operation == "stash-paths": return argv(repo, "--literal-pathspecs", "stash", "push", "-m", "harness-approved-preservation", "--", *paths)
    if operation == "ff-only": return argv(repo, "merge", "--ff-only", "--no-edit", "--no-autostash", "--no-overwrite-ignore", target)
    if operation == "apply-index": return argv(repo, "stash", "apply", "--index", stash)
    raise ValueError("unsupported operation")


def dependencies() -> tuple[tuple[int, int, int, int, str], ...]:
    return tuple(identity(Path(path), single_link=False) for path in (GIT, SSH, UPLOAD_PACK))
