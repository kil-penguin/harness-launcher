"""Fixed local discovery and HTTP transport for harness-herdr-web CLI reads."""

from dataclasses import dataclass
import http.client
import io
import json
import math
import os
from pathlib import Path
import re
import select
import socket
import stat
import time

import harness_herdr_web as web


ROOT_PARTS = (".local", "share", "herdr-web-local")
SECRET_NAME = "access-token"
LOOPBACK = "127.0.0.1"
MANAGED_PORT = 7317
EXTERNAL_ORIGIN = "https://herdr.kilpenguin.com"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
REQUEST_DEADLINE_SECONDS = 10


class DiscoveryError(Exception):
    pass


class TransportError(Exception):
    pass


@dataclass(frozen=True)
class Client:
    token: str
    mode: str
    port: int

    def request_json(self, path, deadline_seconds=None):
        return self._request_json("GET", path, None, deadline_seconds)

    def post_json(self, path, body):
        if not isinstance(body, dict):
            raise TransportError("invalid_request")
        try:
            data = json.dumps(body, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, UnicodeError, ValueError, RecursionError):
            raise TransportError("invalid_request") from None
        if len(data) > 60000 or launch_route("POST", path) == "launch" and len(data) > 4096:
            raise TransportError("request_too_large")
        return self._request_json("POST", path, data)

    def ui_json(self, method, path, body=None, deadline_seconds=None):
        metadata = method == "GET" and re.fullmatch(r"/api/(?:machines/[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}/)?cli/v1/(?:identity|snapshot)", path)
        if ui_route(method, path) is None and agent_ui_request_route(method, path) is None and not metadata:
            raise TransportError("invalid_local_route")
        if method == "GET":
            if body is not None: raise TransportError("invalid_request")
            data = None
        else:
            if not isinstance(body, dict): raise TransportError("invalid_request")
            try: data = json.dumps(body, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
            except (TypeError, UnicodeError, ValueError, RecursionError): raise TransportError("invalid_request") from None
            if len(data) > 65536: raise TransportError("request_too_large")
        return self._request_json(method, path, data, deadline_seconds, max_response_bytes=65536)

    def lifecycle_json(self, method, path, body=None):
        metadata = method == "GET" and re.fullmatch(r"/api/(?:machines/[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}/)?cli/v1/(?:identity|snapshot)", path)
        if lifecycle_route(method, path) is None and not metadata: raise TransportError("invalid_local_route")
        if method == "GET":
            if body is not None: raise TransportError("invalid_request")
            data = None
        else:
            if not isinstance(body, dict): raise TransportError("invalid_request")
            try: data = json.dumps(body, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
            except (TypeError, UnicodeError, ValueError, RecursionError): raise TransportError("invalid_request") from None
            if len(data) > 65536: raise TransportError("request_too_large")
        return self._request_json(method, path, data, max_response_bytes=65536)

    def pane_read_json(self, method, path, body=None, deadline_seconds=None):
        metadata = method == "GET" and re.fullmatch(r"/api/(?:machines/[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}/)?cli/v1/(?:identity|snapshot)", path)
        if not pane_read_route(method, path) and not metadata: raise TransportError("invalid_local_route")
        if method == "GET":
            if body is not None: raise TransportError("invalid_request")
            data = None
        else:
            if not isinstance(body, dict): raise TransportError("invalid_request")
            try: data = json.dumps(body, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
            except (TypeError, UnicodeError, ValueError, RecursionError): raise TransportError("invalid_request") from None
            if len(data) > 65536: raise TransportError("request_too_large")
        return self._request_json(method, path, data, deadline_seconds, max_response_bytes=65536)

    def _request_json(self, method, path, data, deadline_seconds=None, max_response_bytes=None):
        self.url(path)
        if "/pane/read" in path and not pane_read_route(method, path): raise TransportError("invalid_local_route")
        if pane_read_route(method, path): max_response_bytes = 65536
        if "/lifecycle" in path and lifecycle_route(method, path) is None: raise TransportError("invalid_local_route")
        if lifecycle_route(method, path) is not None: max_response_bytes = 65536
        if "/agent/ui-answer" in path and ui_route(method, path) is None:
            raise TransportError("invalid_local_route")
        if "/agent/ui-request" in path and agent_ui_request_route(method, path) is None: raise TransportError("invalid_local_route")
        if ui_route(method, path) is not None or agent_ui_request_route(method, path) is not None: max_response_bytes = 65536
        seconds = REQUEST_DEADLINE_SECONDS if deadline_seconds is None else min(REQUEST_DEADLINE_SECONDS, deadline_seconds)
        if not isinstance(seconds, (int, float)) or seconds <= 0: raise TransportError("deadline_exceeded")
        deadline = time.monotonic() + seconds
        try:
            with socket.create_connection((LOOPBACK, self.port), timeout=remaining(deadline)) as sock:
                sock.setblocking(False)
                send_request(sock, method, path, self.headers(path), data, deadline)
                response = http.client.HTTPResponse(DeadlineSocket(sock, deadline))
                response.begin()
                if pane_read_route(method, path) and response.status == 404: raise TransportError("guard_not_supported")
                if method == "POST" and launch_route(method, path) == "launch" and response.status < 300 and response.status != 202:
                    raise TransportError("http_status")
                try:
                    body = response_body(response, max_response_bytes)
                except TransportError:
                    if pane_read_route(method, path) and response.status >= 300: raise TransportError("guarded_read_unavailable") from None
                    if (ui_route(method, path) is not None or agent_ui_request_route(method, path) is not None or lifecycle_route(method, path) is not None) and response.status >= 300:
                        raise TransportError("guarded_native_unavailable") from None
                    if method == "POST" and "/guarded/" in path and response.status >= 300:
                        raise TransportError(f"http_{response.status}") from None
                    if response.status >= 300 and launch_route(method, path) is not None:
                        raise TransportError("http_status") from None
                    raise
                if response.status >= 300:
                    if pane_read_route(method, path):
                        try: error = strict_json(body.decode("utf-8"), canonical_unsigned=True)
                        except (UnicodeError, ValueError, RecursionError): raise TransportError("guarded_read_unavailable") from None
                        raise TransportError(pane_read_error_code(response.status, error))
                    if lifecycle_route(method, path) is not None:
                        try: error = strict_json(body.decode("utf-8"), canonical_unsigned=True)
                        except (UnicodeDecodeError, ValueError, RecursionError): raise TransportError("guarded_native_unavailable") from None
                        raise TransportError(lifecycle_error_code(response.status, error))
                    if ui_route(method, path) is not None or agent_ui_request_route(method, path) is not None:
                        try: error = strict_json(body.decode("utf-8"))
                        except (UnicodeDecodeError, ValueError, RecursionError): raise TransportError("guarded_native_unavailable") from None
                        raise TransportError(agent_ui_request_error_code(response.status, error) if agent_ui_request_route(method, path) is not None else ui_error_code(response.status, error))
                    if method == "POST" and "/guarded/" in path:
                        try: error = strict_json(body.decode("utf-8"))
                        except (UnicodeDecodeError, ValueError, RecursionError): raise TransportError(f"http_{response.status}") from None
                        code = guarded_error_code(response.status, error)
                        if code is not None: raise TransportError(code)
                        raise TransportError(f"http_{response.status}")
                    if launch_route(method, path) is not None:
                        try: error = strict_json(body.decode("utf-8"))
                        except (UnicodeDecodeError, ValueError, RecursionError): raise TransportError("http_status") from None
                        code = launch_error_code(method, path, response.status, error)
                        if code is not None: raise TransportError(code)
                        raise TransportError("http_status")
                    raise TransportError(f"http_{response.status}")
                return strict_json(body.decode("utf-8"), canonical_unsigned=lifecycle_route(method, path) is not None or pane_read_route(method, path) or agent_ui_request_route(method, path) is not None)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError, RecursionError):
            raise TransportError("invalid_response") from None
        except TransportError:
            raise
        except http.client.HTTPException:
            raise TransportError("invalid_response") from None
        except (TimeoutError, ValueError, OSError):
            if time.monotonic() >= deadline:
                raise TransportError("deadline_exceeded") from None
            raise TransportError("transport_unavailable") from None

    def headers(self, path=""):
        if self.mode == "managed":
            result = {"Authorization": f"Bearer {self.token}", "Host": "herdr.kilpenguin.com",
                    "X-Forwarded-Proto": "https", "Origin": EXTERNAL_ORIGIN, "Accept": "application/json"}
        else:
            origin = f"http://{LOOPBACK}:{self.port}"
            result = {"Authorization": f"Bearer {self.token}", "Host": f"{LOOPBACK}:{self.port}",
                "Origin": origin, "Accept": "application/json"}
        endpoint = launch_route("POST", path)
        if path.startswith("/api/machines/") and ("/cli/v1/guarded/" in path or "/cli/v1/agent/guarded/" in path or endpoint in {"launch-preview", "launch"} or ui_route("GET", path) is not None or ui_route("POST", path) is not None or agent_ui_request_route("GET", path) is not None or agent_ui_request_route("POST", path) is not None or lifecycle_route("POST", path) is not None or pane_read_route("POST", path)): result["X-Herdr-Machine"] = "1"
        if endpoint in {"launch-preview", "launch"}: result["X-Herdr-Launcher"] = "1"
        return result

    def url(self, path):
        if not path.startswith("/api/") or "#" in path or not valid_launch_query(path):
            raise TransportError("invalid_local_route")
        return f"http://{LOOPBACK}:{self.port}{path}"


def remaining(deadline):
    value = deadline - time.monotonic()
    if value <= 0:
        raise TransportError("deadline_exceeded")
    return value


def strict_json(text, canonical_unsigned=False):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result: raise ValueError("duplicate")
            result[key] = value
        return result
    def integer(token):
        if len(token)>20 or re.fullmatch(r"0|[1-9][0-9]*",token) is None or int(token)>(1<<64)-1: raise ValueError("integer")
        return int(token)
    kwargs = {"parse_int": integer, "parse_float": lambda _: (_ for _ in ()).throw(ValueError("integer"))} if canonical_unsigned else {}
    value = json.loads(text, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()), **kwargs)
    pending = [(value, 1)]
    while pending:
        item, depth = pending.pop()
        if depth > 100: raise ValueError("deep")
        if isinstance(item, float) and not math.isfinite(item): raise ValueError("nonfinite")
        if isinstance(item, dict): pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list): pending.extend((child, depth + 1) for child in item)
    return value


def guarded_error_code(status, value):
    allowed = {400: {"invalid_guarded_request"}, 404: {"not_found"},
               409: {"guard_not_supported", "operation_unknown"},
               429: {"operation_admission_full"}, 502: {"guarded_native_unavailable"}}
    if not isinstance(value, dict) or set(value) != {"error"}:
        return None
    error = value["error"]
    if not isinstance(error, dict) or set(error) != {"code", "message"}:
        return None
    if not isinstance(error["code"], str) or not isinstance(error["message"], str):
        return None
    return error["code"] if error["code"] in allowed.get(status, set()) else None


def ui_route(method, path):
    base = r"/api/(?:machines/[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}/)?cli/v1/agent/ui-answer"
    workflow = r"/workflows/[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}"
    routes = (("POST", "prepare", "/prepare"), ("POST", "step", workflow+"/step"), ("GET", "get", workflow), ("POST", "cancel", workflow+"/cancel"))
    return next((name for verb, name, suffix in routes if method == verb and re.fullmatch(base+suffix, path)), None)


def agent_ui_request_route(method, path):
    base = r"/api/(?:machines/[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}/)?cli/v1/agent/ui-request"
    workflow = r"/workflows/[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}"
    routes = (("POST", "prepare", "/prepare"), ("POST", "dispatch", workflow+"/dispatch"), ("GET", "get", workflow), ("POST", "cancel", workflow+"/cancel"))
    return next((name for verb, name, suffix in routes if method == verb and re.fullmatch(base+suffix, path)), None)


def agent_ui_request_error_code(status, value):
    allowed={400:{"invalid_agent_ui_request"},403:{"revoked"},404:{"workflow_unknown","not_found"},409:{"agent_ui_request_unsupported","agent_ui_request_context_unknown","workflow_busy","workflow_conflict","guard_not_supported","operation_unknown"},429:{"operation_admission_full"},502:{"guarded_native_unavailable"}}
    if isinstance(value,dict) and set(value)=={"error"} and isinstance(value["error"],dict) and set(value["error"])=={"code","message"} and isinstance(value["error"].get("code"),str) and isinstance(value["error"].get("message"),str) and value["error"]["code"] in allowed.get(status,set()): return value["error"]["code"]
    return "guarded_native_unavailable"


def ui_error_code(status, value):
    allowed = {400: {"invalid_ui_answer_request"}, 403: {"revoked"}, 404: {"workflow_unknown", "not_found"}, 409: {"ui_answer_unsupported", "prompt_changed", "guard_not_supported", "workflow_busy", "workflow_conflict", "workflow_cancelled", "operation_unknown"}, 429: {"operation_admission_full"}, 502: {"guarded_native_unavailable"}}
    if isinstance(value, dict) and set(value) == {"error"}:
        error = value["error"]
        if isinstance(error, dict) and set(error) == {"code", "message"} and isinstance(error["code"], str) and isinstance(error["message"], str) and error["code"] in allowed.get(status, set()):
            return error["code"]
    return "guarded_native_unavailable"


def launch_route(method, path):
    routes = {("GET", "launch-catalog"), ("GET", "session-catalog"), ("POST", "launch-preview"), ("POST", "launch"), ("GET", "launch-status")}
    raw, _, query = path.partition("?")
    if query and not (method == "GET" and raw.endswith("/workspace/launch-status") and re.fullmatch(r"request_id=[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}", query)):
        return None
    endpoint = next((name for verb, name in routes if verb == method and (raw == f"/api/workspace/{name}" or re.fullmatch(rf"/api/machines/[0-9a-f]{{8}}-(?:[0-9a-f]{{4}}-){{3}}[0-9a-f]{{12}}/workspace/{name}", raw))), None)
    return endpoint


def valid_launch_query(path):
    return "?" not in path or launch_route("GET", path) == "launch-status"


def launch_error_code(method, path, status, value):
    allowed = {
        "launch-catalog": {503: {"launcher_unavailable"}},
        "session-catalog": {403: {"read_only"}, 503: {"launcher_unavailable"}},
        "launch-preview": {400: {"invalid_intent", "invalid_json", "unsupported_launch"}, 403: {"read_only", "invalid_launcher_request"}, 409: {"catalog_changed"}, 413: {"request_too_large"}, 503: {"launch_unavailable", "launcher_unavailable"}},
        "launch": {400: {"invalid_intent", "invalid_json", "unsupported_launch"}, 403: {"read_only", "invalid_launcher_request"}, 409: {"catalog_changed", "request_conflict", "preview_required", "plan_changed", "selection_required", "pane_identity_changed", "shell_not_idle"}, 413: {"request_too_large"}, 503: {"launch_unavailable", "launcher_unavailable", "unsafe_state", "launcher_failed"}},
        "launch-status": {400: {"invalid_request_id"}, 404: {"job_not_found"}, 503: {"unsafe_state", "launcher_unavailable"}},
    }
    endpoint = launch_route(method, path)
    if not isinstance(value, dict) or set(value) != {"error"}:
        return None
    error = value["error"]
    if not isinstance(error, dict) or set(error) != {"code", "message"} or not isinstance(error["code"], str) or not isinstance(error["message"], str):
        return None
    return error["code"] if error["code"] in allowed.get(endpoint, {}).get(status, set()) else None


def send_request(sock, method, path, headers, body, deadline):
    extra = [] if body is None else ["Content-Type: application/json", f"Content-Length: {len(body)}"]
    lines = [f"{method} {path} HTTP/1.1", *[f"{key}: {value}" for key, value in headers.items()], *extra, "Connection: close", "", ""]
    data = "\r\n".join(lines).encode("ascii") + (body or b"")
    while data:
        _, writable, _ = select.select([], [sock], [], remaining(deadline))
        if not writable:
            raise TransportError("deadline_exceeded")
        data = data[sock.send(data):]


def response_body(response, maximum=None):
    maximum = MAX_RESPONSE_BYTES if maximum is None else maximum
    lengths = response.headers.get_all("Content-Length") or []
    transfer = response.headers.get("Transfer-Encoding")
    if len(lengths) > 1 or transfer is not None and (transfer.lower().strip() != "chunked" or lengths):
        raise TransportError("invalid_response")
    if lengths and (not lengths[0].isdecimal() or int(lengths[0]) > maximum):
        raise TransportError("response_too_large")
    body = response.read(maximum + 1)
    if len(body) > maximum:
        raise TransportError("response_too_large")
    return body


class DeadlineSocket:
    def __init__(self, sock, deadline):
        self.sock = sock
        self.deadline = deadline

    def makefile(self, mode):
        return io.BufferedReader(DeadlineFile(self.sock, self.deadline))


class DeadlineFile(io.RawIOBase):
    def __init__(self, sock, deadline):
        self.sock = sock
        self.deadline = deadline

    def readable(self):
        return True

    def readinto(self, buffer):
        readable, _, _ = select.select([self.sock], [], [], remaining(self.deadline))
        if not readable:
            raise TransportError("deadline_exceeded")
        value = self.sock.recv(len(buffer))
        buffer[:len(value)] = value
        return len(value)


def receive(sock, size, deadline):
    readable, _, _ = select.select([sock], [], [], remaining(deadline))
    if not readable:
        raise TransportError("deadline_exceeded")
    value = sock.recv(size)
    if not value:
        raise TransportError("response_closed")
    return value


def discover(home=None):
    base = Path(home) if home is not None else Path.home()
    root = base.joinpath(*ROOT_PARTS)
    if os.path.lexists(root):
        return Client(token=read_managed_token(root), mode="managed", port=MANAGED_PORT)
    return discover_plugin()


def read_managed_token(root):
    root_fd = open_managed_root(root)
    try:
        secret_fd = open_owned_dir("secrets", root_fd, exact_mode=0o700)
        try:
            return read_secret_file(secret_fd)
        finally:
            os.close(secret_fd)
    finally:
        os.close(root_fd)


def open_managed_root(path):
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        fd = os.open(path, flags)
        opened = os.fstat(fd)
    except OSError:
        raise DiscoveryError("managed_discovery_unsafe") from None
    if not stat.S_ISDIR(opened.st_mode) or opened.st_uid != os.getuid() or stat.S_IMODE(opened.st_mode) & 0o022:
        os.close(fd)
        raise DiscoveryError("managed_discovery_unsafe")
    return fd


def open_owned_dir(name, parent_fd, exact_mode):
    try:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        info = os.fstat(fd)
    except OSError:
        raise DiscoveryError("managed_discovery_unsafe") from None
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != exact_mode:
        os.close(fd)
        raise DiscoveryError("managed_discovery_unsafe")
    return fd


def read_secret_file(directory_fd):
    try:
        fd = os.open(SECRET_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=directory_fd)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
            raise DiscoveryError("managed_discovery_unsafe")
        value = read_limited(fd, 256, "managed_discovery_unsafe").decode("ascii").strip()
    except (OSError, UnicodeDecodeError):
        raise DiscoveryError("managed_discovery_unsafe") from None
    finally:
        try:
            os.close(fd)
        except UnboundLocalError:
            pass
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise DiscoveryError("managed_discovery_unsafe")
    return value


def discover_plugin():
    try:
        directory_fd = open_plugin_dir(web.config_dir())
        try:
            env = read_plugin_file(directory_fd, "env")
            dot = read_plugin_file(directory_fd, ".env")
        finally:
            os.close(directory_fd)
        values = web.parse(env)
        values.update(web.parse(dot))
        token = values.get(web.TOKEN_KEY, "")
        port = int(values.get("PORT", web.DEFAULT_PORT))
    except (web.Refused, OSError, ValueError, UnicodeError):
        raise DiscoveryError("plugin_discovery_unsafe") from None
    if len(token) < web.MIN_TOKEN_LEN or values.get(web.HOST_KEY) != LOOPBACK or port != MANAGED_PORT:
        raise DiscoveryError("plugin_discovery_unsafe")
    return Client(token=token, mode="plugin", port=port)


def open_plugin_dir(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(fd)
    except OSError:
        raise DiscoveryError("plugin_discovery_unsafe") from None
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        os.close(fd)
        raise DiscoveryError("plugin_discovery_unsafe")
    return fd


def read_plugin_file(directory_fd, name):
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=directory_fd)
    except FileNotFoundError:
        return ""
    except OSError:
        raise DiscoveryError("plugin_discovery_unsafe") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
            raise DiscoveryError("plugin_discovery_unsafe")
        return read_limited(fd, 65536, "plugin_discovery_unsafe").decode("utf-8", "replace")
    finally:
        os.close(fd)


def read_limited(fd, limit, error_code):
    chunks = []
    remaining = limit + 1
    while remaining:
        chunk = os.read(fd, min(8192, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    value = b"".join(chunks)
    if remaining == 0:
        raise DiscoveryError(error_code)
    return value


def lifecycle_route(method, path):
    base = r"/api/(?:machines/[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}/)?cli/v1/lifecycle/"
    return next((name for name in ("prepare", "create", "interrupt", "close", "operation", "operation/cancel") if method == "POST" and re.fullmatch(base+name, path)), None)


def lifecycle_error_code(status, value):
    pairs = {400: {"lifecycle_invalid_request": "Invalid lifecycle request"}, 429: {"lifecycle_admission_full": "Lifecycle admission is full"}, 409: {"lifecycle_operation_unknown": "Lifecycle operation is unknown", "guard_not_supported": "Guarded input is not supported"}, 502: {"guarded_native_unavailable": "Guarded Native is unavailable"}, 403: {"revoked": "Drive access is unavailable", "read_only": "This device can only watch"}, 404: {"not_found": "not found"}}
    if isinstance(value, dict) and set(value) == {"error"}:
        error = value["error"]
        if isinstance(error, dict) and set(error) == {"code", "message"} and isinstance(error["code"], str) and isinstance(error["message"], str) and error["message"] == pairs.get(status, {}).get(error["code"]): return error["code"]
    return "guarded_native_unavailable"


def pane_read_route(method, path):
    return method == "POST" and re.fullmatch(r"/api/(?:machines/[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}/)?cli/v1/pane/read", path) is not None


def pane_read_error_code(status, value):
    pairs = {400: {"invalid_request": "Invalid guarded pane read request"}, 409: {"guard_mismatch": "guard target no longer matches", "guard_not_supported": "Guarded input is not supported"}, 502: {"guarded_read_unavailable": "Guarded pane read is unavailable", "guarded_native_unavailable": "Guarded Native is unavailable"}, 403: {"revoked": "Drive access is unavailable", "read_only": "This device can only watch"}}
    if isinstance(value, dict) and set(value) == {"error"}:
        error = value["error"]
        if isinstance(error, dict) and set(error) == {"code", "message"} and isinstance(error["code"], str) and isinstance(error["message"], str) and error["message"] == pairs.get(status, {}).get(error["code"]): return error["code"]
    return "guarded_read_unavailable"
