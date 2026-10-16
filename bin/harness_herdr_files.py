"""Fail-closed readers for caller-owned private target and payload files."""
import contextlib
import json
import math
import os
import stat

MAX_BYTES = 64 * 1024
MAX_JSON_DEPTH = 100


class FileError(Exception):
    pass


def _fail():
    raise FileError("private_file_invalid")


def _limit(value):
    if type(value) is not int or not 0 < value <= MAX_BYTES:
        _fail()


def _components(path):
    if not isinstance(path, str) or not path.startswith("/"):
        _fail()
    parts = path.split("/")[1:]
    if not parts or any(part in {"", ".", ".."} for part in parts):
        _fail()
    return parts


def _open_directory(parent, name):
    fd = None
    try:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            os.close(fd)
            fd = None
            _fail()
        return fd
    except FileError:
        raise
    except OSError:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        _fail()


@contextlib.contextmanager
def _private_file(path, max_bytes):
    _limit(max_bytes)
    parts = _components(path)
    directories = []
    try:
        directories.append(os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC))
        for component in parts[:-1]:
            directories.append(_open_directory(directories[-1], component))
        parent = directories[-1]
        info = os.fstat(parent)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            _fail()
        try:
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
        except OSError:
            _fail()
        try:
            info = os.fstat(fd)
            if not (stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                    and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o600):
                _fail()
            yield fd
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
    except FileError:
        raise
    except (OSError, UnicodeError, ValueError):
        _fail()
    finally:
        for fd in reversed(directories):
            try:
                os.close(fd)
            except OSError:
                pass


def _read(path, max_bytes):
    with _private_file(path, max_bytes) as fd:
        content = b""
        try:
            while len(content) <= max_bytes:
                part = os.read(fd, min(8192, max_bytes + 1 - len(content)))
                if not part:
                    break
                content += part
        except OSError:
            _fail()
    if len(content) > max_bytes:
        _fail()
    return content


def read_private_text(path, max_bytes):
    content = _read(path, max_bytes)
    if b"\0" in content:
        _fail()
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        _fail()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail()
        result[key] = value
    return result


def _bounded_depth(value):
    pending = [(value, 1)]
    while pending:
        item, depth = pending.pop()
        if depth > MAX_JSON_DEPTH:
            _fail()
        if isinstance(item, dict):
            for key, child in item.items():
                _strict_string(key)
                pending.append((child, depth + 1))
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
        elif isinstance(item, str):
            _strict_string(item)
        elif isinstance(item, float) and not math.isfinite(item):
            _fail()


def _strict_string(value):
    if "\0" in value:
        _fail()
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        _fail()


def read_private_json_object(path, max_bytes):
    content = read_private_text(path, max_bytes)
    try:
        result = json.loads(content, object_pairs_hook=_pairs, parse_constant=lambda _value: _fail())
    except FileError:
        raise
    except Exception:
        _fail()
    if not isinstance(result, dict):
        _fail()
    _bounded_depth(result)
    return result
