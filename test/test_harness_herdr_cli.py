#!/usr/bin/env python3
"""Read-only CLI contract with synthetic loopback Web responses."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch


BIN = Path(__file__).resolve().parents[1] / "bin" / "harness-herdr-web"
sys.path.insert(0, str(BIN.parent))
import harness_herdr_cli as cli
import harness_herdr_local as local

TOKEN = "a" * 64
LOCAL = "11111111-1111-4111-8111-111111111111"
REMOTE = "22222222-2222-4222-8222-222222222222"


class Server:
    def __init__(self):
        self.calls = []
        self.redirects = {}
        self.trickles = {}
        self.chunked = {}
        self.trailers = {}
        self.extra_headers = {}
        self.continues = set()
        self.routes = {
            "/api/cli/v1/identity": {"protocol": 1, "device_id": LOCAL,
                "capabilities": {"read_metadata_v1": True, "guarded_mutation_v1": False},
                "native": {"version": "0.9.1"}},
            "/api/machines": {"machines": [
                {"id": "local", "name": "Mini", "state": "connected", "role": "drive", "cwd": "DO_NOT_LEAK"},
                {"id": REMOTE, "name": "Laptop", "state": "connected", "role": "watch", "argv": "DO_NOT_LEAK"},
            ]},
            "/api/cli/v1/snapshot": {"protocol": 1, "device_id": LOCAL, "server_epoch": "epoch-1",
                "workspaces": [{"workspace_id": "ws-1"}], "tabs": [{"tab_id": "tab-1", "workspace_id": "ws-1"}],
                "panes": [{"pane_id": "pane-1", "terminal_id": "term-1", "workspace_id": "ws-1", "tab_id": "tab-1", "agent": None, "agent_status": "idle"}]},
            f"/api/machines/{REMOTE}/cli/v1/snapshot": {"protocol": 1, "device_id": REMOTE,
                "server_epoch": "epoch-2", "workspaces": [{"workspace_id": "ws-2", "cwd": "DO_NOT_LEAK"}],
                "tabs": [{"tab_id": "tab-2", "workspace_id": "ws-2", "title": "DO_NOT_LEAK"}],
                "panes": [{"pane_id": "pane-2", "terminal_id": "term-2", "workspace_id": "ws-2", "tab_id": "tab-2", "agent": "codex", "agent_status": "ready", "session": "DO_NOT_LEAK"}]},
            "/api/too-large": b"x" * (2 * 1024 * 1024 + 1),
        }
        self.post_routes = {}

        outer = self
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.calls.append((self.path, dict(self.headers)))
                if self.path in outer.redirects:
                    self.send_response(302)
                    self.send_header("Location", outer.redirects[self.path])
                    self.end_headers()
                    return
                if self.path in outer.continues:
                    self.wfile.write(b"HTTP/1.1 100 Continue\r\n\r\n")
                if self.path in outer.trickles:
                    body, delay = outer.trickles[self.path]
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    for byte in body:
                        try:
                            self.wfile.write(bytes((byte,))); self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError):
                            return
                        time.sleep(delay)
                    return
                if self.path in outer.chunked:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    for part in outer.chunked[self.path]:
                        self.wfile.write(f"{len(part):x}\r\n".encode() + part + b"\r\n")
                    trailers = b"".join(f"{key}: {value}\r\n".encode() for key, value in outer.trailers.get(self.path, []))
                    self.wfile.write(b"0\r\n" + trailers + b"\r\n")
                    return
                body = outer.routes.get(self.path)
                if body is None:
                    self.send_response(404); self.end_headers(); return
                status = 200
                if isinstance(body, tuple): status, body = body
                encoded = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                for key, value in outer.extra_headers.get(self.path, []):
                    self.send_header(key, value)
                self.end_headers()
                try:
                    self.wfile.write(encoded)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            def do_POST(self):
                size = int(self.headers.get("Content-Length", "0"))
                outer.calls.append((self.path, dict(self.headers), self.rfile.read(size)))
                value = outer.post_routes.get(self.path, {"ok": True})
                status = 200
                if isinstance(value, tuple): status, value = value
                encoded = value if isinstance(value, bytes) else json.dumps(value).encode()
                self.send_response(status); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded))); self.end_headers()
                try:
                    self.wfile.write(encoded)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            def log_message(self, *args):
                pass
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown(); self.httpd.server_close()


class CliReadTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "home"; self.home.mkdir()
        root = self.home / ".local/share/herdr-web-local"
        self.secrets = root / "secrets"; self.secrets.mkdir(parents=True)
        self.secrets.chmod(0o700)
        self.token = self.secrets / "access-token"; self.token.write_text(TOKEN + "\n"); self.token.chmod(0o600)
        self.server = Server(); self.addCleanup(self.server.close)

    def client(self):
        return local.Client(token=TOKEN, mode="managed", port=self.server.port)

    def test_devices_resolves_local_alias_and_uses_external_tuple(self):
        data = cli.execute(["devices"], client=self.client())
        self.assertEqual(data["devices"][0]["id"], LOCAL)
        self.assertEqual(data["devices"][0], {"id": LOCAL, "name": "Mini", "state": "connected", "role": "drive"})
        _, headers = self.server.calls[-1]
        self.assertEqual(headers["Host"], "herdr.kilpenguin.com")
        self.assertEqual(headers["X-Forwarded-Proto"], "https")
        self.assertEqual(headers["Origin"], "https://herdr.kilpenguin.com")

    def test_post_json_sends_only_json_to_guarded_route(self):
        self.assertEqual(self.client().post_json("/api/cli/v1/guarded/prepare", {"method": "x"}), {"ok": True})
        path, headers, body = self.server.calls[-1]
        self.assertEqual(path, "/api/cli/v1/guarded/prepare")
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(json.loads(body), {"method": "x"})

    def test_guarded_post_rejects_malformed_json_and_marks_remote(self):
        path = f"/api/machines/{REMOTE}/cli/v1/guarded/input"
        for value in (b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}', b'{"x":-Infinity}', b'{"x":1e999}', b'\xff', (b'{"x":' * 1001 + b'0' + b'}' * 1001)):
            self.server.post_routes[path] = value
            with self.assertRaisesRegex(local.TransportError, "invalid_response"):
                self.client().post_json(path, {"x": 1})
        _, headers, _ = self.server.calls[-1]
        self.assertEqual(headers["X-Herdr-Machine"], "1")

    def test_remote_agent_guarded_post_marks_machine_boundary(self):
        path = f"/api/machines/{REMOTE}/cli/v1/agent/guarded/prepare"
        self.assertEqual(self.client().post_json(path, {"method": "agent.guarded_prompt.v1"}), {"ok": True})
        sent_path, headers, body = self.server.calls[-1]
        self.assertEqual(sent_path, path)
        self.assertEqual(headers["X-Herdr-Machine"], "1")
        self.assertEqual(json.loads(body), {"method": "agent.guarded_prompt.v1"})

    def test_post_rejects_nonfinite_and_oversize_before_network(self):
        client = self.client()
        for body in ({"x": float("nan")}, {"x": float("inf")}, {"x": "x" * 60001}):
            with self.assertRaisesRegex(local.TransportError, "invalid_request|request_too_large"):
                client.post_json("/api/cli/v1/guarded/input", body)
        self.assertEqual(self.server.calls, [])

    def test_guarded_operation_unknown_discards_message_and_legacy_get_keeps_status(self):
        path = "/api/cli/v1/guarded/operation"
        self.server.post_routes[path] = (409, {"error": {"code": "operation_unknown", "message": "SECRET"}})
        with self.assertRaisesRegex(local.TransportError, "^operation_unknown$"):
            self.client().post_json(path, {"operation_id": LOCAL, "server_epoch": "e"})
        self.server.routes["/api/legacy-503"] = (503, b"{}")
        # The GET path still preserves its established transport status code.
        with self.assertRaisesRegex(local.TransportError, "^http_503$"):
            self.client().request_json("/api/legacy-503")

    def test_guarded_error_mapping_projects_only_exact_operation_unknown(self):
        path = "/api/cli/v1/guarded/operation"
        cases = (
            (409, {"error": {"code": "unknown_code", "message": "SECRET", "data": "SECRET"}}),
            (400, {"error": {"code": "operation_unknown", "message": "SECRET", "data": "SECRET"}}),
            (409, b'{"error":{"code":"operation_unknown","code":"SECRET"}}'),
            (409, b"SECRET"),
            (409, b"S" * (2 * 1024 * 1024 + 1)),
            (409, {"error": []}),
            (409, {"error": "SECRET"}),
            (409, {"error": None}),
            (409, {"error": {"code": [], "message": "SECRET"}}),
            (409, {"error": {"code": {}, "message": "SECRET"}}),
            (409, {"error": {"code": "operation_unknown", "message": "SECRET", "data": "SECRET"}}),
            (409, {"error": {"code": "operation_unknown", "message": "SECRET"}, "extra": "SECRET"}),
            (409, {"error": {"code": "operation_unknown"}}),
            (409, {"error": {"code": "operation_unknown", "message": []}}),
        )
        for status, body in cases:
            self.server.post_routes[path] = (status, body)
            with self.assertRaisesRegex(local.TransportError, f"^http_{status}$") as raised:
                self.client().post_json(path, {"operation_id": LOCAL, "server_epoch": "e"})
            self.assertNotIn("SECRET", str(raised.exception))
        self.server.post_routes[path] = (409, {"error": {"code": "operation_unknown", "message": "SECRET"}})
        with self.assertRaisesRegex(local.TransportError, "^operation_unknown$") as raised:
            self.client().post_json(path, {"operation_id": LOCAL, "server_epoch": "e"})
        self.assertNotIn("SECRET", str(raised.exception))

    def test_remote_workspaces_uses_full_uuid_route_and_drops_unknown_fields(self):
        data = cli.execute(["workspaces", "--device", REMOTE], client=self.client())
        self.assertEqual(data, {"device_id": REMOTE, "server_epoch": "epoch-2", "workspaces": [{"workspace_id": "ws-2"}]})
        self.assertEqual(self.server.calls[-1][0], f"/api/machines/{REMOTE}/cli/v1/snapshot")

    def test_self_tabs_uses_direct_route_and_only_membership(self):
        data = cli.execute(["tabs", "--device", LOCAL, "--workspace", "ws-1"], client=self.client())
        self.assertEqual(data, {"device_id": LOCAL, "server_epoch": "epoch-1", "tabs": [{"tab_id": "tab-1", "workspace_id": "ws-1"}]})
        self.assertEqual(self.server.calls[-1][0], "/api/cli/v1/snapshot")

    def test_unsafe_managed_install_does_not_fall_back(self):
        self.secrets.chmod(0o755)
        with self.assertRaisesRegex(local.DiscoveryError, "managed_discovery_unsafe"):
            local.discover(home=self.home)

    def test_plugin_fallback_refuses_nonfixed_port(self):
        values = {local.web.TOKEN_KEY: TOKEN, local.web.HOST_KEY: "127.0.0.1", "PORT": "7400"}
        with patch.object(local.web, "config_dir", return_value=self.home), patch.object(local.web, "files", return_value=(self.home / "env", self.home / ".env")), patch.object(local.web, "effective", return_value=values):
            with self.assertRaisesRegex(local.DiscoveryError, "plugin_discovery_unsafe"):
                local.discover_plugin()

    def test_managed_root_and_plugin_hardlink_fail_closed(self):
        root = self.home / ".local/share/herdr-web-local"
        root.chmod(0o777)
        with self.assertRaisesRegex(local.DiscoveryError, "managed_discovery_unsafe"):
            local.discover(home=self.home)
        root.chmod(0o755)
        plugin = self.home / "plugin"; plugin.mkdir(); plugin.chmod(0o700)
        env = plugin / "env"; env.write_text(f"HERDR_WEB_TOKEN={TOKEN}\nHOST=127.0.0.1\nPORT=7317\n"); env.chmod(0o600)
        linked = plugin / "env-linked"; os.link(env, linked)
        with patch.object(local.web, "config_dir", return_value=plugin):
            with self.assertRaisesRegex(local.DiscoveryError, "plugin_discovery_unsafe"):
                local.discover_plugin()

    def test_plugin_fifo_is_refused_without_blocking(self):
        plugin = self.home / "fifo-plugin"; plugin.mkdir(); plugin.chmod(0o700)
        os.mkfifo(plugin / "env", 0o600)
        with patch.object(local.web, "config_dir", return_value=plugin):
            with self.assertRaisesRegex(local.DiscoveryError, "plugin_discovery_unsafe"):
                local.discover_plugin()

    def test_managed_fifo_is_refused_without_blocking(self):
        self.token.unlink()
        os.mkfifo(self.token, 0o600)
        with self.assertRaisesRegex(local.DiscoveryError, "managed_discovery_unsafe"):
            local.discover(home=self.home)

    def test_managed_and_plugin_symlinks_fail_closed(self):
        target = self.home / "token-source"; target.write_text(TOKEN); target.chmod(0o600)
        self.token.unlink(); self.token.symlink_to(target)
        with self.assertRaisesRegex(local.DiscoveryError, "managed_discovery_unsafe"):
            local.discover(home=self.home)
        plugin = self.home / "symlink-plugin"; plugin.mkdir(); plugin.chmod(0o700)
        env = plugin / "env"; env.symlink_to(target)
        with patch.object(local.web, "config_dir", return_value=plugin):
            with self.assertRaisesRegex(local.DiscoveryError, "plugin_discovery_unsafe"):
                local.discover_plugin()

    def test_plugin_config_requires_bounded_eof(self):
        plugin = self.home / "large-plugin"; plugin.mkdir(); plugin.chmod(0o700)
        env = plugin / "env"; env.write_bytes(b"x" * 65537); env.chmod(0o600)
        with patch.object(local.web, "config_dir", return_value=plugin):
            with self.assertRaisesRegex(local.DiscoveryError, "plugin_discovery_unsafe"):
                local.discover_plugin()

    def test_missing_identity_route_is_capability_unsupported_without_raw_error(self):
        self.server.routes.pop("/api/cli/v1/identity")
        with self.assertRaisesRegex(cli.CommandError, "capability_unsupported"):
            cli.execute(["devices"], client=self.client())

    def test_response_size_and_full_uuid_shape_fail_closed(self):
        with self.assertRaisesRegex(local.TransportError, "response_too_large"):
            self.client().request_json("/api/too-large")
        with self.assertRaisesRegex(cli.CommandError, "invalid_device_id"):
            cli.exact_uuid(LOCAL.replace("-", ""))

    def test_trickle_response_cannot_extend_absolute_deadline(self):
        self.server.trickles["/api/trickle"] = (b'{"a":1}', 0.04)
        started = time.monotonic()
        with patch.object(local, "REQUEST_DEADLINE_SECONDS", 0.1):
            with self.assertRaisesRegex(local.TransportError, "deadline_exceeded"):
                self.client().request_json("/api/trickle")
        self.assertLess(time.monotonic() - started, 0.2)

    def test_chunked_http_response_uses_standard_framing(self):
        self.server.chunked["/api/chunked"] = [b'{"a"', b":1}"]
        self.server.trailers["/api/chunked"] = [("X-Fixture", "done")]
        self.assertEqual(self.client().request_json("/api/chunked"), {"a": 1})

    def test_standard_parser_handles_continue_and_rejects_duplicate_content_length(self):
        self.server.routes["/api/continue"] = b"{}"
        self.server.continues.add("/api/continue")
        self.assertEqual(self.client().request_json("/api/continue"), {})
        self.server.routes["/api/duplicate"] = b"{}"
        self.server.extra_headers["/api/duplicate"] = [("Content-Length", "2")]
        with self.assertRaisesRegex(local.TransportError, "invalid_response"):
            self.client().request_json("/api/duplicate")

    def test_non_utf8_json_is_invalid_response(self):
        self.server.routes["/api/cli/v1/identity"] = b'\xff'
        with self.assertRaisesRegex(cli.CommandError, "invalid_response"):
            cli.execute(["devices"], client=self.client())

    def test_malformed_json_is_an_invalid_response_without_body_echo(self):
        self.server.routes["/api/cli/v1/identity"] = b'{"secret":"DO_NOT_ECHO"'
        with self.assertRaisesRegex(cli.CommandError, "invalid_response"):
            cli.execute(["devices"], client=self.client())

    def test_redirect_and_proxy_never_send_token_to_second_listener(self):
        second = Server(); self.addCleanup(second.close)
        self.server.redirects["/api/redirect"] = f"http://127.0.0.1:{second.port}/token-sink"
        with self.assertRaisesRegex(local.TransportError, "http_302"):
            self.client().request_json("/api/redirect")
        self.assertEqual(second.calls, [])
        with patch.dict(os.environ, {"http_proxy": f"http://127.0.0.1:{second.port}"}):
            cli.execute(["devices"], client=self.client())
        self.assertEqual(second.calls, [])

    def test_missing_epoch_and_missing_workspace_are_invalid_or_not_found(self):
        snapshot = self.server.routes[f"/api/machines/{REMOTE}/cli/v1/snapshot"]
        snapshot.pop("server_epoch")
        with self.assertRaisesRegex(cli.CommandError, "invalid_response"):
            cli.execute(["workspaces", "--device", REMOTE], client=self.client())
        snapshot["server_epoch"] = "epoch-2"
        with self.assertRaisesRegex(cli.CommandError, "target_not_found"):
            cli.execute(["tabs", "--device", REMOTE, "--workspace", "missing"], client=self.client())

    def test_malformed_identity_wrong_snapshot_uuid_and_control_bytes_fail_closed(self):
        self.server.routes["/api/cli/v1/identity"]["device_id"] = "bad"
        with self.assertRaisesRegex(cli.CommandError, "invalid_response"):
            cli.execute(["devices"], client=self.client())
        self.server.routes["/api/cli/v1/identity"]["device_id"] = LOCAL
        snapshot = self.server.routes[f"/api/machines/{REMOTE}/cli/v1/snapshot"]
        snapshot["device_id"] = LOCAL
        with self.assertRaisesRegex(cli.CommandError, "invalid_response"):
            cli.execute(["workspaces", "--device", REMOTE], client=self.client())

    def test_snapshot_requires_agent_and_parent_membership(self):
        snapshot = self.server.routes[f"/api/machines/{REMOTE}/cli/v1/snapshot"]
        snapshot["panes"][0].pop("agent")
        with self.assertRaisesRegex(cli.CommandError, "invalid_response"):
            cli.execute(["agents", "--device", REMOTE, "--pane", "pane-2"], client=self.client())
        snapshot["panes"][0]["agent"] = "codex"
        snapshot["tabs"][0]["workspace_id"] = "missing-workspace"
        with self.assertRaisesRegex(cli.CommandError, "invalid_response"):
            cli.execute(["workspaces", "--device", REMOTE], client=self.client())
        snapshot["tabs"][0]["workspace_id"] = "ws-2"
        snapshot["panes"][0].pop("workspace_id")
        with self.assertRaisesRegex(cli.CommandError, "invalid_response"):
            cli.execute(["workspaces", "--device", REMOTE], client=self.client())
        snapshot["device_id"] = REMOTE
        snapshot["panes"][0]["terminal_id"] = "term\x1b2"
        with self.assertRaisesRegex(cli.CommandError, "invalid_response"):
            cli.execute(["workspaces", "--device", REMOTE], client=self.client())

    def test_public_device_metadata_rejects_control_bytes(self):
        self.server.routes["/api/machines"]["machines"][0]["name"] = "Mini\x1b[2J"
        self.assertEqual(cli.execute(["devices"], client=self.client()), {"devices": [
            {"id": REMOTE, "name": "Laptop", "state": "connected", "role": "watch"}
        ]})

    def test_unknown_device_state_is_not_a_roster_projection(self):
        self.server.routes["/api/machines"]["machines"][0]["state"] = "terminal-prompt"
        self.assertEqual(cli.execute(["devices"], client=self.client()), {"devices": [
            {"id": REMOTE, "name": "Laptop", "state": "connected", "role": "watch"}
        ]})

    def test_wrapper_exposes_cli_help(self):
        fresh_home = Path(self.temp.name) / "fresh-home"; fresh_home.mkdir()
        environment = {**os.environ, "HOME": str(fresh_home)}
        result = subprocess.run([str(BIN), "cli", "--help"], text=True, capture_output=True, env=environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = subprocess.run([str(BIN), "cli", "unknown"], text=True, capture_output=True, env=environment)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
