#!/usr/bin/env python3
"""Inherit allowlisted native Codex plugins into a broker-bound isolated home.

Only a package the canonical home has installed and enabled, plus its two config
tables, is carried. Hook trust, auth and other runtime state never are, so the
user keeps native /hooks review. Nothing this run did not create is replaced or
removed; any doubt leaves the home unchanged for the harness audit to report.
"""
import ctypes
import errno
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import stat
import string
import sys
import tomllib

ID = re.compile(r"[a-z0-9][a-z0-9_-]*@[a-z0-9][a-z0-9_-]*")
VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*")
MANAGED_MARKETPLACES = {"openai-bundled"}
MAX_ENTRIES, MAX_DEPTH, MAX_FILE, MAX_TOTAL, MAX_CONFIG = 2000, 16, 8 << 20, 32 << 20, 4 << 20
DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
# Both live inside plugins/, which Native history already treats as excluded.
# Each install stages in its own fresh holder; a crashed run's holder is left.
STAGING_PREFIX = ".harness-inherit-"
RECORD = ".harness-inherited.json"
UNAVAILABLE = {errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EINVAL}
TEMP_ALPHABET = string.ascii_letters + string.digits
EXCLUSIVE = (0x4, 0x1)  # Darwin RENAME_EXCL, Linux RENAME_NOREPLACE
SWAP = (0x2, 0x2)  # Darwin RENAME_SWAP, Linux RENAME_EXCHANGE
EXPECTED = (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError, UnicodeError)


class Skip(Exception):
    """State that must not be inherited."""


def _before_install(market_fd):
    """Test seam between the existence check and the exclusive rename."""


def _before_swap(home_fd):
    """Test seam between reading live config and publishing the candidate."""


def _before_record(plugins_fd):
    """Test seam between reading and publishing the inheritance record."""


def _trust():
    spec = importlib.util.spec_from_file_location(
        "codex_hook_trust_plugins", Path(__file__).with_name("codex-hook-trust.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _checked(fd):
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        os.close(fd)
        raise Skip("unsafe directory")
    return fd


def _open_dir(parent, name, create=False, mode=0o755):
    if create:
        try:
            os.mkdir(name, mode, dir_fd=parent)
        except FileExistsError:
            pass
    return _checked(os.open(name, DIRECTORY, dir_fd=parent))


def _fstat(fd):
    return os.fstat(fd)


def _stat(path):
    return os.lstat(path)


def _verified_root_identity(recorded, target):
    """Open the session root, check that very fd against the record, return its identity.

    Same inode and either the same device or a whole-volume device renumbering
    where the root is on the device of its worktrees directory.
    """
    fields = recorded.split()
    if len(fields) != 2 or not all(field.isascii() and field.isdigit() for field in fields):
        raise Skip("session root replaced")
    device, inode = int(fields[0]), int(fields[1])
    fd = _checked(os.open(target, DIRECTORY | os.O_NOFOLLOW))
    try:
        info = _fstat(fd)
        if info.st_ino != inode or (info.st_dev != device and _stat(Path(target).parent).st_dev != info.st_dev):
            raise Skip("session root replaced")
        return info.st_dev, info.st_ino
    finally:
        os.close(fd)


def _open_path(root, parts, create=False, root_identity=None):
    fd = _checked(os.open(root, DIRECTORY))
    try:
        if root_identity is not None:
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) != root_identity:
                raise Skip("session root replaced after verification")
        for part in parts:
            child = _open_dir(fd, part, create)
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd


def _read_file(parent, name, limit):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022
                or info.st_size > limit):
            raise Skip("unsafe file")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise Skip("file grew")
    return data, info


def _read_tree(fd, depth=0, budget=None):
    """Read a package once through directory descriptors, rejecting any link."""
    budget = {"entries": 0, "bytes": 0} if budget is None else budget
    if depth > MAX_DEPTH:
        raise Skip("package too deep")
    entries = {}
    for name in sorted(os.listdir(fd)):
        budget["entries"] += 1
        if budget["entries"] > MAX_ENTRIES:
            raise Skip("package has too many entries")
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise Skip("unsafe package entry")
        if stat.S_ISDIR(info.st_mode):
            child = _checked(os.open(name, DIRECTORY, dir_fd=fd))
            try:
                entries[name] = _read_tree(child, depth + 1, budget)
            finally:
                os.close(child)
        elif stat.S_ISREG(info.st_mode):
            data, opened = _read_file(fd, name, MAX_FILE)
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise Skip("package changed while reading")
            budget["bytes"] += len(data)
            if budget["bytes"] > MAX_TOTAL:
                raise Skip("package too large")
            entries[name] = (bool(opened.st_mode & 0o111), data)
        else:
            raise Skip("package contains a link or special file")
    return entries


def _signature(entries):
    return {name: _signature(value) if isinstance(value, dict)
            else (value[0], hashlib.sha256(value[1]).hexdigest())
            for name, value in entries.items()}


def _write_tree(fd, entries):
    for name, value in entries.items():
        if isinstance(value, dict):
            os.mkdir(name, 0o755, dir_fd=fd)
            child = _checked(os.open(name, DIRECTORY, dir_fd=fd))
            try:
                _write_tree(child, value)
            finally:
                os.close(child)
            continue
        executable, data = value
        mode = 0o755 if executable else 0o644
        out = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=fd)
        with os.fdopen(out, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)


def _flagged_rename():
    """Return the flagged rename primitive; there is deliberately no plain-rename fallback."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin" and hasattr(libc, "renameatx_np"):
        return libc.renameatx_np, 0
    if sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        return libc.renameat2, 1
    raise Skip("exclusive rename unavailable")


def _rename(src_fd, src, dst_fd, dst, flags):
    function, index = _flagged_rename()
    result = function(src_fd, os.fsencode(src), dst_fd, os.fsencode(dst), ctypes.c_uint(flags[index]))
    if result != 0:
        error = ctypes.get_errno()
        if error in UNAVAILABLE:
            raise Skip("exclusive rename unavailable")
        raise OSError(error, os.strerror(error), f"{src} -> {dst}")


def _discard(dir_fd, name):
    try:
        os.unlink(name, dir_fd=dir_fd)
    except FileNotFoundError:
        pass


def _swap_in(dir_fd, temporary, name, expected):
    """Compare-and-swap the staged `temporary` over `name` and own its cleanup.

    Returns True once published. A concurrent writer's file is put back. The
    staged name is removed only while it holds candidate data or the expected
    replaced file; if putting a file back fails, it is left for recovery.
    """
    if expected is None:
        try:
            _rename(dir_fd, temporary, dir_fd, name, EXCLUSIVE)
        except BaseException as error:
            _discard(dir_fd, temporary)
            if isinstance(error, OSError) and error.errno in (errno.EEXIST, errno.ENOTEMPTY):
                return False
            raise
        return True
    try:
        _rename(dir_fd, temporary, dir_fd, name, SWAP)
    except BaseException:
        _discard(dir_fd, temporary)
        raise
    try:
        matched = _identity(dir_fd, temporary) == expected
    except Exception:
        matched = False
    if matched:
        _discard(dir_fd, temporary)
        return True
    _rename(dir_fd, temporary, dir_fd, name, SWAP)
    _discard(dir_fd, temporary)
    return False


def _allowlist(root, trust):
    manifest = json.loads(trust.regular(root / "config/codex-surface.json", root).decode())
    values = manifest["runtime_audit"]["native_codex_plugins"]
    if (not isinstance(values, list) or len(set(values)) != len(values)
            or any(not isinstance(value, str) or not ID.fullmatch(value) for value in values)):
        raise Skip("invalid runtime_audit.native_codex_plugins policy")
    return values


def _source_package(source, plugin_id, trust):
    plugin, marketplace = plugin_id.split("@")
    config = tomllib.loads(trust.regular(source / ".harness/codex/config.toml", source).decode())
    if (config.get("plugins") or {}).get(plugin_id) != {"enabled": True}:
        raise Skip("not installed and enabled in the canonical home")
    market = (config.get("marketplaces") or {}).get(marketplace)
    if (not isinstance(market, dict) or set(market) != {"source_type", "source"}
            or market["source_type"] != "local" or not isinstance(market["source"], str)
            or not os.path.isabs(market["source"]) or os.path.normpath(market["source"]) != market["source"]
            or not os.path.isdir(market["source"])):
        raise Skip("canonical marketplace is not an exact local definition")
    fd = _open_path(source, (".harness", "codex", "plugins", "cache", marketplace, plugin))
    try:
        versions = [name for name in os.listdir(fd) if not name.startswith(".")]
        if len(versions) != 1 or not VERSION.fullmatch(versions[0]):
            raise Skip("canonical cache must hold exactly one version")
        version = versions[0]
        version_fd = _checked(os.open(version, DIRECTORY, dir_fd=fd))
        try:
            package = _read_tree(version_fd)
        finally:
            os.close(version_fd)
    finally:
        os.close(fd)
    manifest = json.loads(package[".codex-plugin"]["plugin.json"][1])
    if manifest.get("name") != plugin or manifest.get("version") != version:
        raise Skip("canonical package identity mismatch")
    return market, version, {version: package}


def _read_live(home_fd):
    data, info = _read_file(home_fd, "config.toml", MAX_CONFIG)
    return data.decode(), (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns), stat.S_IMODE(info.st_mode)


def _identity(parent, name):
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return None
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _read_record(home, home_fd):
    """Suppression state only: a forged or unsafe record can block, never add."""
    path = home / "plugins" / RECORD
    try:
        plugins_fd = _checked(os.open("plugins", DIRECTORY, dir_fd=home_fd))
    except FileNotFoundError:
        return {}, None
    try:
        identity = _identity(plugins_fd, RECORD)
        if identity is None:
            return {}, None
        try:
            data, _info = _read_file(plugins_fd, RECORD, 64 * 1024)
            record = json.loads(data)
            inherited = record["inherited"]
            if (set(record) != {"schema_version", "inherited"} or record["schema_version"] != 1
                    or not isinstance(inherited, dict)
                    or any(not ID.fullmatch(key) or not isinstance(value, str) or not VERSION.fullmatch(value)
                           for key, value in inherited.items())):
                raise Skip("schema")
        except (Skip, *EXPECTED) as error:
            raise Skip(f"unsafe or malformed inheritance record {path}") from error
        return inherited, identity
    finally:
        os.close(plugins_fd)


def _write_record(plugins_fd, inherited, expected):
    temporary = f"{RECORD}.{secrets.token_hex(6)}"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=plugins_fd)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"schema_version": 1, "inherited": inherited}, stream, sort_keys=True)
            stream.write("\n")
        _before_record(plugins_fd)
    except BaseException:
        _discard(plugins_fd, temporary)
        raise
    if not _swap_in(plugins_fd, temporary, RECORD, expected):
        raise Skip("inheritance record changed concurrently; left untouched")


def _install(plugins_fd, cache_fd, plugin_id, entries, session_cache):
    plugin, marketplace = plugin_id.split("@")
    market_fd = _open_dir(cache_fd, marketplace, create=True)
    try:
        try:
            existing = _checked(os.open(plugin, DIRECTORY, dir_fd=market_fd))
        except FileNotFoundError:
            existing = None
        if existing is not None:
            try:
                current = _signature(_read_tree(existing))
            finally:
                os.close(existing)
            if current != _signature(entries):
                raise Skip(f"existing session cache {session_cache} differs; left untouched "
                           "(remove that directory or start a new session to inherit)")
            return
        _flagged_rename()
        # The staged package itself moves into place, so success leaves nothing
        # behind. A failed attempt leaves its staged directory; nothing is deleted.
        staged = STAGING_PREFIX + secrets.token_hex(8)
        os.mkdir(staged, 0o700, dir_fd=plugins_fd)
        package_fd = _checked(os.open(staged, DIRECTORY, dir_fd=plugins_fd))
        try:
            _write_tree(package_fd, entries)
            _before_install(market_fd)
            _rename(plugins_fd, staged, market_fd, plugin, EXCLUSIVE)
            # Private while staged; the installed directory matches its siblings.
            os.fchmod(package_fd, 0o755)
        finally:
            os.close(package_fd)
    finally:
        os.close(market_fd)


def _publish_config(home_fd, installed):
    """Append tables with a compare-and-swap; live decisions made meanwhile win."""
    for _attempt in range(5):
        text, expected, mode = _read_live(home_fd)
        live = tomllib.loads(text)
        plugins = live.get("plugins") or {}
        markets = live.get("marketplaces") or {}
        blocks, added, emitted = [], {}, set()
        for plugin_id, (market, version) in installed.items():
            marketplace = plugin_id.split("@")[1]
            if plugin_id in plugins or markets.get(marketplace, market) != market:
                continue
            if marketplace not in markets and marketplace not in emitted:
                blocks.append(f"[marketplaces.{marketplace}]\nsource_type = \"local\"\n"
                              f"source = {json.dumps(market['source'], ensure_ascii=False)}\n")
                emitted.add(marketplace)
            # The comment sits inside the table: a line above it would join the
            # previous table, such as the launcher's own project trust entry.
            blocks.append(f"[plugins.{json.dumps(plugin_id)}]\n# Inherited from the canonical home;"
                          " hook trust stays with native review.\nenabled = true\n")
            added[plugin_id] = version
        if not added:
            return {}
        candidate = text.rstrip("\n") + "\n\n" + "\n".join(blocks)
        tomllib.loads(candidate)
        temporary = ".config.toml." + "".join(secrets.choice(TEMP_ALPHABET) for _ in range(6))
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=home_fd)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), mode)
                stream.write(candidate)
            _before_swap(home_fd)
        except BaseException:
            _discard(home_fd, temporary)
            raise
        if _swap_in(home_fd, temporary, "config.toml", expected):
            return added
    raise Skip("live config kept changing")


def ensure(published, trust=None):
    """Return notices; never raises for expected evidence or filesystem states."""
    notices = []
    try:
        trust = trust or _trust()
        published = Path(published)
        source, target, _base, state = trust.broker_source(published)
        if published != target / ".harness/codex":
            raise Skip("not the bound session home")
        # Every later write is relative to a root fd proven to be the recorded inode.
        recorded = trust.regular(state / "sessions" / target.name / "root-inode", state).decode()
        root_identity = _verified_root_identity(recorded, target)
        canonical = _allowlist(source, trust)
        wanted = [plugin_id for plugin_id in _allowlist(target, trust)
                  if plugin_id in canonical and plugin_id.split("@")[1] not in MANAGED_MARKETPLACES]
        if not wanted:
            return notices
        home_fd = _open_path(target, (".harness", "codex"), root_identity=root_identity)
    except (Skip, *EXPECTED) as error:
        return [f"native Codex plugins not inherited: {error}"]
    try:
        text, _live_identity, _mode = _read_live(home_fd)
        live = tomllib.loads(text)
        missing = [plugin_id for plugin_id in wanted if plugin_id not in (live.get("plugins") or {})]
        if not missing:
            return notices
        record, record_identity = _read_record(published, home_fd)
        candidates = {}
        for plugin_id in missing:
            try:
                if plugin_id in record:
                    raise Skip("removed in this session; not inherited again")
                market, version, entries = _source_package(source, plugin_id, trust)
                current = (live.get("marketplaces") or {}).get(plugin_id.split("@")[1])
                if current is not None and current != market:
                    raise Skip("session marketplace differs; left untouched")
                candidates[plugin_id] = (market, version, entries)
            except (Skip, *EXPECTED) as error:
                notices.append(f"native Codex plugin {plugin_id} not inherited: {error}")
        if not candidates:
            return notices
        _flagged_rename()  # Before creating anything: without it nothing is inherited.
        plugins_fd = _open_dir(home_fd, "plugins", create=True)
        try:
            installed = {}
            cache_fd = _open_dir(plugins_fd, "cache", create=True)
            try:
                for plugin_id, (market, version, entries) in candidates.items():
                    plugin, marketplace = plugin_id.split("@")
                    try:
                        _install(plugins_fd, cache_fd, plugin_id, entries,
                                 published / "plugins/cache" / marketplace / plugin)
                        installed[plugin_id] = (market, version)
                    except (Skip, *EXPECTED) as error:
                        notices.append(f"native Codex plugin {plugin_id} not inherited: {error}")
            finally:
                os.close(cache_fd)
            if installed:
                added = _publish_config(home_fd, installed)
                if added:
                    _write_record(plugins_fd, {**record, **added}, record_identity)
        finally:
            os.close(plugins_fd)
    except (Skip, *EXPECTED) as error:
        notices.append(f"native Codex plugins not inherited: {error}")
    finally:
        os.close(home_fd)
    return notices


def main(argv):
    if len(argv) != 3 or argv[1] != "ensure":
        print("usage: codex-plugin-inherit.py ensure <isolated-codex-home>", file=sys.stderr)
        return 2
    for notice in ensure(Path(argv[2])):
        print(f"harness-launcher: {notice}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
