#!/usr/bin/env python3
"""Company usage-collection helpers for the launcher (stdlib only).

Subcommands
  encode PATH                       Claude project-directory name for PATH
  codex-otel CONFIG_TOML            company [otel] table, whitelisted, as TOML
  sync-session --state-home S --session-id ID --active 0|1
  reconcile --state-home S [--harness DIR=0|1 ...]
  source-roots --state-home S       distinct session source roots under HOME

The decision (ACTIVE) is made once, in harness-common.sh
(harness_agent_usage_policy); this module only renders it. A session whose
source root was not evaluated is left untouched, never defaulted to inactive.
Several state homes may share one collection.d: the orphan sweep removes only
manifests that belong to the state home being reconciled. reconcile and
sync-session hold ~/.agent-usage/.harness-launcher.lock (bounded wait, then
skip fail-open). Manifests follow the collector's `collection.d` schema 1.
Nothing here prints config values.
"""

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import time
from pathlib import Path

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
PREFIX_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
MANIFEST_PREFIX = "harness-launcher-"
OTEL_KEYS = ("environment", "log_user_prompt", "exporter", "trace_exporter", "metrics_exporter")
# Claude Code truncates longer sanitized names and appends a hash (inferred, not
# verified); a 200-character prefix still matches by the collector's rule.
CLAUDE_NAME_CAP = 200
LOCK_NAME = ".harness-launcher.lock"
LOCK_WAIT_SECONDS = 2.0
# Per-item failures: one manifest or record never stops the rest of the run.
ITEM_ERRORS = (OSError, ValueError, UnicodeDecodeError, AttributeError, TypeError)


def warn(message):
    print(f"WARN: harness-launcher: {message}", file=sys.stderr)


def test_mode():
    return os.environ.get("HARNESS_AGENT_USAGE_TEST_MODE") == "1"


def claude_encode(path):
    out = []
    for char in str(path):
        if char.isascii() and char.isalnum():
            out.append(char)
        else:
            out.append("-" * (len(char.encode("utf-16-le")) // 2))
    return "".join(out)


def claude_prefix(path):
    name = claude_encode(path)[:CLAUDE_NAME_CAP]
    return name if re.fullmatch(r"-[A-Za-z0-9-]{1,255}", name) else None


# ---- Codex [otel] ---------------------------------------------------------

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _key(name):
    return name if _BARE_KEY.match(name) else json.dumps(name, ensure_ascii=False)


def _value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(_value(item) for item in value) + "]"
    if isinstance(value, dict):
        if not value:
            return "{}"
        return "{ " + ", ".join(f"{_key(k)} = {_value(v)}" for k, v in value.items()) + " }"
    raise ValueError("unsupported TOML value")


def codex_otel(config_path):
    """Exit codes: 0 block printed, 4 no file / no [otel], 5 unreadable."""
    import tomllib

    try:
        with open(config_path, "rb") as stream:
            config = tomllib.load(stream)
    except FileNotFoundError:
        return 4
    except (OSError, tomllib.TOMLDecodeError):
        return 5
    table = config.get("otel")
    if not isinstance(table, dict):
        return 4
    kept = {key: table[key] for key in OTEL_KEYS if key in table}
    if not kept:
        return 4
    try:
        lines = ["[otel]"] + [f"{_key(k)} = {_value(v)}" for k, v in kept.items()]
    except ValueError:
        return 5
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


# ---- manifests ------------------------------------------------------------


def agent_usage_dir(home):
    if test_mode() and os.environ.get("HARNESS_AGENT_USAGE_STATE_DIR"):
        return Path(os.environ["HARNESS_AGENT_USAGE_STATE_DIR"])
    return Path(home) / ".agent-usage"


def private_dir(directory):
    """Create DIRECTORY (0700) when missing; refuse links and foreign owners."""
    if directory.is_symlink():
        raise OSError("unsafe collection directory")
    if not directory.exists():
        directory.mkdir(mode=0o700)
    info = os.lstat(directory)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise OSError("unsafe collection directory")
    if stat.S_IMODE(info.st_mode) & 0o022:
        os.chmod(directory, 0o700)
    return directory


def collection_dir(home):
    """Create ~/.agent-usage/collection.d (0700) when missing; refuse links."""
    return private_dir(private_dir(agent_usage_dir(home)) / "collection.d")


@contextlib.contextmanager
def launcher_lock(home):
    """Cross-process lock beside (never inside) collection.d, which the
    collector reads. Yields False when it stays busy for LOCK_WAIT_SECONDS."""
    path = private_dir(agent_usage_dir(home)) / LOCK_NAME
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise OSError("unsafe lock file")
        if stat.S_IMODE(info.st_mode) != 0o600:
            os.fchmod(fd, 0o600)
        deadline = time.monotonic() + LOCK_WAIT_SECONDS
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(0.05)
        yield True
    finally:
        os.close(fd)


def write_manifest(directory, name, manifest):
    """Atomic 0600 write; untouched when the content is unchanged."""
    payload = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
    target = directory / name
    try:
        if target.read_bytes() == payload and stat.S_IMODE(os.lstat(target).st_mode) == 0o600 and not target.is_symlink():
            return False
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(prefix=".harness-launcher.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as out:
            os.fchmod(out.fileno(), 0o600)
            out.write(payload)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return True


def remove_manifest(directory, name):
    try:
        os.unlink(directory / name)
        return True
    except FileNotFoundError:
        return False


def under(path, home):
    path, home = os.path.realpath(path), os.path.realpath(home)
    return path == home or path.startswith(home.rstrip("/") + "/")


def claude_projects_dir():
    config = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return Path(config) / "projects"


def claude_project_exists(prefix, names):
    return any(name == prefix or name.startswith(prefix + "-") for name in names)


def claude_project_names():
    try:
        return [entry.name for entry in os.scandir(claude_projects_dir())]
    except OSError:
        return []


def exclude_entries(root):
    """Exclusions for a root: its Codex home and its Claude project dirs."""
    entries, seen = [], set()
    for variant in dict.fromkeys([os.path.abspath(root), os.path.realpath(root)]):
        for entry in (
            {"path": os.path.join(variant, ".harness", "codex")},
            {"provider": "claude", "projectDirPrefix": claude_prefix(variant)},
        ):
            key = json.dumps(entry, sort_keys=True)
            if key in seen or entry.get("projectDirPrefix", "x") is None:
                continue
            seen.add(key)
            entries.append(entry)
    return entries


def session_manifest(session_id, worktree, archive, active, home, project_names):
    """Manifest body for one session, or None when nothing is left to say."""
    sid = session_id.lower()
    label = "hl-" + sid.replace("-", "")[:12]
    wt_live = os.path.isdir(worktree) and not os.path.islink(worktree)
    if active:
        base = None
        if wt_live:
            real = os.path.realpath(worktree)
            roots = [(os.path.join(real, ".harness", "codex", "sessions"), label),
                     (os.path.join(real, ".harness", "codex", "archived_sessions"), label + "-a")]
            exists = False
        else:
            if not os.path.isdir(archive) or os.path.islink(archive):
                return None
            real = os.path.realpath(archive)
            roots = [(os.path.join(real, "codex", "sessions"), label),
                     (os.path.join(real, "codex", "archived_sessions"), label + "-a")]
            exists = True
        include = [{"provider": "codex", "root": root, "label": lab}
                   for root, lab in roots
                   if under(root, home) and (not exists or os.path.isdir(root))]
        return {"schema": 1, "include": include, "exclude": []} if include else None
    exclude = exclude_entries(worktree)
    if not wt_live:
        prefix = claude_prefix(os.path.realpath(worktree))
        prefixes = {e.get("projectDirPrefix") for e in exclude if e.get("projectDirPrefix")}
        if not any(claude_project_exists(p, project_names) for p in prefixes | ({prefix} if prefix else set())):
            return None
    return {"schema": 1, "include": [], "exclude": exclude}


def read_line(path):
    try:
        with open(path, "r", encoding="utf-8") as stream:
            return stream.readline().rstrip("\r\n")
    except OSError:
        return ""


def sync_one(directory, state_home, session_id, active, home, project_names):
    name = f"{MANIFEST_PREFIX}{session_id.lower()}.json"
    session_dir = Path(state_home) / "sessions" / session_id
    worktree = read_line(session_dir / "session-root") or str(Path(state_home) / "worktrees" / session_id)
    archive = str(Path(state_home) / "archives" / session_id)
    manifest = session_manifest(session_id, worktree, archive, active, home, project_names)
    if manifest is None:
        remove_manifest(directory, name)
        return
    write_manifest(directory, name, manifest)


def realpath_or(path):
    return os.path.realpath(path) if path else ""


def parse_prefix(harness_dir):
    try:
        with open(os.path.join(harness_dir, "config", "launcher.env"), encoding="utf-8") as stream:
            for line in stream:
                if line.startswith("HARNESS_PREFIX="):
                    value = line.rstrip("\r\n")[len("HARNESS_PREFIX="):].strip("\"'")
                    return value if PREFIX_RE.match(value) and len(value) <= 80 else None
    except OSError:
        pass
    return None


def session_entries(state_home):
    """UUID-named, non-link session record directories of STATE_HOME."""
    try:
        entries = sorted(os.scandir(Path(state_home) / "sessions"), key=lambda entry: entry.name)
    except OSError:
        return []
    return [entry for entry in entries
            if UUID_RE.match(entry.name) and not entry.is_symlink() and entry.is_dir()]


def source_roots(state_home, home):
    """Distinct recorded source roots under HOME (the policy is evaluated for
    each in harness-common.sh, independent of any in-process registry)."""
    roots = []
    if not under(state_home, home):
        return roots
    for entry in session_entries(state_home):
        try:
            source = read_line(os.path.join(entry.path, "source-root"))
        except ITEM_ERRORS:
            continue
        if source and "\n" not in source and source not in roots and os.path.isdir(source) and under(source, home):
            roots.append(source)
    return roots


def owned_by_state(data, state_home):
    """True when every path the manifest names lies under STATE_HOME (and it
    names at least one), and every Claude prefix is one of its worktrees."""
    real_state = os.path.realpath(state_home)
    items = list(data.get("include", [])) + list(data.get("exclude", []))
    if not all(isinstance(item, dict) for item in items):
        return False
    paths = [item.get("root") for item in data.get("include", [])]
    paths += [item.get("path") for item in data.get("exclude", []) if "path" in item]
    if not paths or not all(isinstance(path, str) and under(path, real_state) for path in paths):
        return False
    state_prefixes = [p for p in {claude_prefix(real_state), claude_prefix(os.path.abspath(state_home))} if p]
    for item in data.get("exclude", []):
        if "projectDirPrefix" in item:
            prefix = item["projectDirPrefix"]
            if not isinstance(prefix, str) or not any(prefix.startswith(p) for p in state_prefixes):
                return False
    return True


def session_record_exists(state_home, session_id):
    sessions = Path(state_home) / "sessions"
    return any(os.path.lexists(sessions / name) for name in {session_id, session_id.upper()})


def sweep_one(directory, state_home, name, names):
    """Remove an orphaned session manifest of this state home, unless it is a
    Claude exclusion that still has a transcript directory to hide."""
    data = json.loads((directory / name).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not owned_by_state(data, state_home):
        return
    session_id = name[len(MANIFEST_PREFIX):-len(".json")]
    # Re-checked right before removal: a session created meanwhile keeps it.
    if not UUID_RE.match(session_id) or session_record_exists(state_home, session_id):
        return
    prefixes = [e.get("projectDirPrefix") for e in data.get("exclude", [])]
    if data.get("include") or not any(p and claude_project_exists(p, names) for p in prefixes):
        remove_manifest(directory, name)


def write_profile(directory, path, active):
    """Profile exclusion for an evaluated harness with a valid prefix. It
    persists once written and goes only when that harness evaluates active."""
    prefix = parse_prefix(path)
    if not prefix:
        return
    name = f"{MANIFEST_PREFIX}profile-{prefix}.json"
    if active:
        remove_manifest(directory, name)
    else:
        write_manifest(directory, name, {"schema": 1, "include": [], "exclude": exclude_entries(path)})


def each(items, action, label):
    """Run ACTION per item; a failing item warns on one line and is skipped."""
    for item in items:
        try:
            action(item)
        except ITEM_ERRORS as error:
            warn(f"agent-usage {label} skipped ({error.__class__.__name__})")


def reconcile(state_home, home, harnesses):
    # The collector accepts include roots only under the home directory and
    # scans no harness outside it. Without an evaluated harness under this home
    # (a test fixture under $TMPDIR, even one that inherited the real state home)
    # there is no policy to apply. A state home outside the home has no
    # sessions this home's manifests describe.
    # One entry per real path; the first (a registered or launched dir, which
    # may be a link) wins over the same root read from a session record.
    unique = {}
    for path, active in harnesses:
        if under(path, home):
            unique.setdefault(realpath_or(path), (path, active))
    harnesses = list(unique.values())
    if not harnesses:
        return
    with launcher_lock(home) as locked:
        if not locked:
            warn("agent-usage manifests not updated (another launcher holds the lock)")
            return
        reconcile_locked(state_home, home, harnesses)


def reconcile_locked(state_home, home, harnesses):
    own_state = under(state_home, home)
    directory = collection_dir(home)
    policy = {realpath_or(path): active for path, active in harnesses}
    names = claude_project_names()
    entries = session_entries(state_home) if own_state else []
    known = {f"{MANIFEST_PREFIX}{entry.name.lower()}.json" for entry in entries}

    def sync_entry(entry):
        source = realpath_or(read_line(os.path.join(entry.path, "source-root")))
        if source in policy:  # an unevaluated source is left untouched
            sync_one(directory, state_home, entry.name, policy[source], home, names)

    each(entries, sync_entry, "session manifest")
    orphans = [entry.name for entry in os.scandir(directory) if own_state
               and entry.name.startswith(MANIFEST_PREFIX) and entry.name.endswith(".json")
               and not entry.name.startswith(MANIFEST_PREFIX + "profile-") and entry.name not in known]
    each(orphans, lambda name: sweep_one(directory, state_home, name, names), "orphan manifest")
    each(harnesses, lambda item: write_profile(directory, *item), "profile manifest")


def sync_session(state_home, home, session_id, active):
    source = read_line(os.path.join(state_home, "sessions", session_id, "source-root"))
    if not under(state_home, home) or not source or not under(source, home):
        return
    with launcher_lock(home) as locked:
        if not locked:
            warn("agent-usage session manifest not updated (another launcher holds the lock)")
            return
        sync_one(collection_dir(home), state_home, session_id, active, home, claude_project_names())


def main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    enc = sub.add_parser("encode")
    enc.add_argument("path")
    otel = sub.add_parser("codex-otel")
    otel.add_argument("config")
    one = sub.add_parser("sync-session")
    one.add_argument("--state-home", required=True)
    one.add_argument("--session-id", required=True)
    one.add_argument("--active", choices=("0", "1"), required=True)
    rec = sub.add_parser("reconcile")
    rec.add_argument("--state-home", required=True)
    rec.add_argument("--harness", action="append", default=[])
    roots = sub.add_parser("source-roots")
    roots.add_argument("--state-home", required=True)
    args = parser.parse_args(argv)
    home = os.path.expanduser("~")
    if args.command == "encode":
        print(claude_encode(args.path))
        return 0
    if args.command == "codex-otel":
        return codex_otel(args.config)
    if args.command == "source-roots":
        for root in source_roots(args.state_home, home):
            print(root)
        return 0
    try:
        if args.command == "sync-session":
            if not UUID_RE.match(args.session_id):
                return 2
            sync_session(args.state_home, home, args.session_id, args.active == "1")
        else:
            harnesses = []
            for item in args.harness:
                path, _, flag = item.rpartition("=")
                if flag in ("0", "1") and path:
                    harnesses.append((path, flag == "1"))
            reconcile(args.state_home, home, harnesses)
    except ITEM_ERRORS as error:
        warn(f"agent-usage manifest not updated ({error.__class__.__name__})")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
