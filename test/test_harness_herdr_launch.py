import io
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "bin"))
import harness_herdr_cli as cli
import harness_herdr_launch_receipt as receipt

DEVICE = "11111111-1111-4111-8111-111111111111"


class Client:
    def __init__(self):
        self.calls = []

    def request_json(self, path):
        self.calls.append(path)
        return {"available": True, "revision": "r1", "profiles": [
            {"id": "sample", "label": "Sample", "directory": "/SECRET", "runtimes": [
                {"id": "codex", "label": "Codex", "presets": [{"id": "base", "label": "Base", "model": "m", "effort": "medium"}]}]}]}


class LaunchCliTest(unittest.TestCase):
    def test_job_accepts_assigned_ids_before_started_without_projecting_them(self):
        request = "33333333-3333-4333-8333-333333333333"
        for phase in ("allocating", "preparing", "starting", "unknown", "failed"):
            with self.subTest(phase=phase):
                self.assertEqual(cli.launch._job({"request_id": request, "phase": phase,
                                                   "workspace_id": "workspace-safe", "pane_id": "pane-safe"}, request),
                                 {"phase": phase})

    def test_job_rejects_non_string_phase_and_untyped_optional_protocol_fields(self):
        request = "33333333-3333-4333-8333-333333333333"
        malformed = (
            {"request_id": request, "phase": []},
            {"request_id": request, "phase": "unknown", "session_id": []},
            {"request_id": request, "phase": "unknown", "runtime_revision": False},
            {"request_id": request, "phase": "unknown", "error": {"unrecognized": "SECRET"}},
        )
        for value in malformed:
            with self.subTest(value=value):
                with self.assertRaisesRegex(cli.launch.LaunchError, "invalid_response"):
                    cli.launch._job(value, request)

    def test_job_accepts_source_error_codes_without_projecting_message(self):
        request = "33333333-3333-4333-8333-333333333333"
        for code in ("allocation_unknown", "observation_interrupted", "catalog_changed"):
            self.assertEqual(cli.launch._job({"request_id": request, "phase": "unknown", "error": {"code": code, "message": "SECRET"}}, request), {"phase": "unknown"})

    def test_main_launch_uses_exact_local_and_remote_post_then_status_get(self):
        seen, requests = [], []
        catalog = {"available": True, "revision": "r1", "profiles": [{"id": "sample", "label": "Sample", "directory": "/SECRET", "runtimes": [{"id": "codex", "label": "C", "presets": [{"id": "base", "label": "B", "model": "m", "effort": "e"}]}]}]}
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append((self.path, dict(self.headers)))
                value = catalog if self.path.endswith("launch-catalog") else {"request_id": requests[-1]["request_id"], "phase": "started", "workspace_id": "workspace-safe", "pane_id": "pane-safe"}
                body = json.dumps(value).encode(); self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
            def do_POST(self):
                value = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(value); seen.append((self.path, dict(self.headers)))
                body = json.dumps({"request_id": value["request_id"], "phase": "starting"}).encode(); self.send_response(202); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
            def log_message(self, *args): pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler); threading.Thread(target=server.serve_forever, daemon=True).start(); self.addCleanup(lambda: (server.shutdown(), server.server_close()))
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "home"; (home / ".local/state").mkdir(parents=True)
            for path in (home, home / ".local", home / ".local/state"): path.chmod(0o700)
            store = receipt.LaunchReceiptStore(home=home)
            for device, local_id in ((DEVICE, DEVICE), ("22222222-2222-4222-8222-222222222222", DEVICE)):
                output = io.StringIO(); client = cli.local.Client(token="a" * 64, mode="plugin", port=server.server_address[1])
                with patch.object(cli.local, "discover", return_value=client), patch.object(cli, "read_identity", return_value={"device_id": local_id}), patch.object(cli.receipt, "LaunchReceiptStore", return_value=store), patch("sys.stdout", output):
                    self.assertEqual(cli.main(["launch", "--device", device, "--catalog-revision", "r1", "--profile", "sample", "--runtime", "codex", "--preset", "base", "--plan-digest", "a" * 64, "--new-request"]), 0)
                self.assertNotIn("SECRET", output.getvalue())
        posts = [row for row in seen if row[0].endswith("/workspace/launch")]
        statuses = [row for row in seen if "/workspace/launch-status?request_id=" in row[0]]
        self.assertEqual(len(posts), 2); self.assertEqual(len(statuses), 2)
        self.assertNotIn("X-Herdr-Launcher", statuses[0][1]); self.assertNotIn("X-Herdr-Machine", statuses[1][1])
        self.assertEqual(posts[0][1]["X-Herdr-Launcher"], "1"); self.assertNotIn("X-Herdr-Machine", posts[0][1])
        self.assertEqual(posts[1][1]["X-Herdr-Launcher"], "1"); self.assertEqual(posts[1][1]["X-Herdr-Machine"], "1")

    def test_launch_output_flush_failure_never_posts(self):
        catalog = {"available": True, "revision": "r1", "profiles": [{"id": "sample", "label": "Sample", "directory": "/SECRET", "runtimes": [{"id": "codex", "label": "Codex", "presets": [{"id": "base", "label": "Base", "model": "m", "effort": "e"}]}]}]}
        client, posts = Client(), []
        client.request_json = lambda path: catalog
        client.post_json = lambda *args: posts.append(args)
        request = {"request_id": "33333333-3333-4333-8333-333333333333", "catalog_revision": "r1", "profile": "sample", "runtime": "codex", "preset": "base", "plan_digest": "a" * 64}
        class BrokenOutput:
            def write(self, value): return len(value)
            def flush(self): raise BrokenPipeError("closed")
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "home"; (home / ".local/state").mkdir(parents=True)
            for path in (home, home / ".local", home / ".local/state"): path.chmod(0o700)
            with self.assertRaises(BrokenPipeError):
                cli.launch.start(client, DEVICE, DEVICE, request, BrokenOutput(), receipts=receipt.LaunchReceiptStore(home=home))
        self.assertEqual(posts, [])

    def test_launch_freshly_validates_then_posts_once_and_projects_receipt(self):
        catalog = {"available": True, "revision": "r1", "profiles": [{"id": "sample", "label": "Sample", "directory": "/SECRET", "runtimes": [{"id": "codex", "label": "Codex", "presets": [{"id": "base", "label": "Base", "model": "m", "effort": "e"}]}]}]}
        client, output, posts = Client(), io.StringIO(), []
        client.request_json = lambda path: catalog if path.endswith("launch-catalog") else {"request_id": posts[0][1]["request_id"], "phase": "started", "workspace_id": "workspace-safe", "pane_id": "pane-safe"}
        client.post_json = lambda path, body: (posts.append((path, body)) or {"request_id": body["request_id"], "phase": "started", "workspace_id": "workspace-safe", "pane_id": "pane-safe"})
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "home"; (home / ".local/state").mkdir(parents=True)
            for path in (home, home / ".local", home / ".local/state"): path.chmod(0o700)
            store = receipt.LaunchReceiptStore(home=home)
            with patch.object(cli.local, "discover", return_value=client), patch.object(cli, "read_identity", return_value={"device_id": DEVICE}), patch.object(cli.receipt, "LaunchReceiptStore", return_value=store), patch("sys.stdout", output):
                self.assertEqual(cli.main(["launch", "--device", DEVICE, "--catalog-revision", "r1", "--profile", "sample", "--runtime", "codex", "--preset", "base", "--plan-digest", "a" * 64, "--new-request"]), 0)
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][0], "/api/workspace/launch")
        value = json.loads(output.getvalue().splitlines()[-1])
        self.assertEqual(value["phase"], "started")
        self.assertEqual({key for key in value}, {"intent", "device_id", "request_id", "phase", "workspace_id", "pane_id"})
        self.assertNotIn("SECRET", output.getvalue())

    def test_launch_observes_same_request_with_status_get_after_one_post(self):
        catalog = {"available": True, "revision": "r1", "profiles": [{"id": "sample", "label": "Sample", "directory": "/SECRET", "runtimes": [{"id": "codex", "label": "Codex", "presets": [{"id": "base", "label": "Base", "model": "m", "effort": "e"}]}]}]}
        request_id = "33333333-3333-4333-8333-333333333333"
        client, output, posts, gets = Client(), io.StringIO(), [], []
        def get(path):
            gets.append(path)
            return catalog if path.endswith("launch-catalog") else {"request_id": request_id, "phase": "started", "workspace_id": "workspace-safe", "pane_id": "pane-safe"}
        client.request_json = get
        client.post_json = lambda path, body: (posts.append((path, body)) or {"request_id": request_id, "phase": "starting"})
        request = {"request_id": request_id, "catalog_revision": "r1", "profile": "sample", "runtime": "codex", "preset": "base", "plan_digest": "a" * 64}
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "home"; (home / ".local/state").mkdir(parents=True)
            for path in (home, home / ".local", home / ".local/state"): path.chmod(0o700)
            value = cli.launch.start(client, DEVICE, DEVICE, request, output, receipts=receipt.LaunchReceiptStore(home=home))
        self.assertEqual(len(posts), 1)
        self.assertEqual(gets, ["/api/workspace/launch-catalog", f"/api/workspace/launch-status?request_id={request_id}"])
        self.assertEqual(value["phase"], "started")

    def test_launch_status_recovers_saved_unknown_intent_with_get_only(self):
        remote = "22222222-2222-4222-8222-222222222222"
        client, output, calls = Client(), io.StringIO(), []
        request_id = "33333333-3333-4333-8333-333333333333"
        client.request_json = lambda path: (calls.append(path) or {"request_id": request_id, "phase": "unknown", "error": {"code": "start_unknown", "message": "SECRET"}})
        client.post_json = lambda *args: self.fail("status must not POST")
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "home"; (home / ".local/state").mkdir(parents=True)
            for path in (home, home / ".local", home / ".local/state"): path.chmod(0o700)
            store = receipt.LaunchReceiptStore(home=home)
            intent = "44444444-4444-4444-8444-444444444444"
            store.create(intent, remote, request_id)
            value = cli.launch.status(client, intent, DEVICE, output, receipts=store)
        self.assertEqual(calls, [f"/api/machines/{remote}/workspace/launch-status?request_id={request_id}"])
        self.assertEqual(value["phase"], "unknown")
        self.assertNotIn("SECRET", output.getvalue())

    def test_status_observation_deadline_preserves_unknown_without_repost(self):
        request_id = "33333333-3333-4333-8333-333333333333"
        client, output = Client(), io.StringIO()
        client.request_json = lambda path: {"request_id": request_id, "phase": "starting"}
        client.post_json = lambda *args: self.fail("observation must not POST")
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "home"; (home / ".local/state").mkdir(parents=True)
            for path in (home, home / ".local", home / ".local/state"): path.chmod(0o700)
            store = receipt.LaunchReceiptStore(home=home)
            intent = "44444444-4444-4444-8444-444444444444"
            store.create(intent, DEVICE, request_id)
            with patch.object(cli.launch.time, "monotonic", side_effect=(0, 240)):
                value = cli.launch.status(client, intent, DEVICE, output, receipts=store)
        self.assertEqual(value["phase"], "unknown")

    def test_status_clock_expiry_before_sleep_never_passes_negative_duration(self):
        request_id = "33333333-3333-4333-8333-333333333333"
        client, output = Client(), io.StringIO()
        client.request_json = lambda path: {"request_id": request_id, "phase": "starting"}
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "home"; (home / ".local/state").mkdir(parents=True)
            for path in (home, home / ".local", home / ".local/state"): path.chmod(0o700)
            store = receipt.LaunchReceiptStore(home=home); intent = "44444444-4444-4444-8444-444444444444"
            store.create(intent, DEVICE, request_id)
            with patch.object(cli.launch.time, "monotonic", side_effect=(0, 0, 0, 239.5, 239.5, 240.1)):
                value = cli.launch.status(client, intent, DEVICE, output, receipts=store)
        self.assertEqual(value["phase"], "unknown")

    def test_main_local_and_remote_preview_use_loopback_headers_without_secret_output(self):
        seen = []
        catalog = {"available":True,"revision":"r1","profiles":[{"id":"sample","label":"Sample","directory":"/SECRET","runtimes":[{"id":"codex","label":"C","presets":[{"id":"base","label":"B","model":"m","effort":"e"}]}]}]}
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append((self.path, dict(self.headers))); body=json.dumps(catalog).encode(); self.send_response(200); self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body)
            def do_POST(self):
                seen.append((self.path, dict(self.headers))); body=json.dumps({"catalog_revision":"r1","plan_digest":"a"*64,"can_launch":True,"summary":{"runtime":"codex","preset":"base","model":"m","effort":"e","context":"x"},"launcher_args":["SECRET"]}).encode(); self.send_response(200); self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body)
            def log_message(self,*args): pass
        server=ThreadingHTTPServer(("127.0.0.1",0),Handler); threading.Thread(target=server.serve_forever,daemon=True).start(); self.addCleanup(lambda:(server.shutdown(),server.server_close()))
        for device, local_id in ((DEVICE,DEVICE),("22222222-2222-4222-8222-222222222222",DEVICE)):
            out=io.StringIO(); client=cli.local.Client(token="a"*64,mode="plugin",port=server.server_address[1])
            with patch.object(cli.local,"discover",return_value=client),patch.object(cli,"read_identity",return_value={"device_id":local_id}),patch("sys.stdout",out):
                self.assertEqual(cli.main(["launch-preview","--device",device,"--catalog-revision","r1","--profile","sample","--runtime","codex","--preset","base"]),0)
            self.assertNotIn("SECRET",out.getvalue())
        posts=[row for row in seen if row[0].endswith("launch-preview")]
        self.assertEqual(posts[0][1]["X-Herdr-Launcher"],"1"); self.assertNotIn("X-Herdr-Machine",posts[0][1])
        self.assertEqual(posts[1][1]["X-Herdr-Launcher"],"1"); self.assertEqual(posts[1][1]["X-Herdr-Machine"],"1")
    def test_loopback_preview_headers_keep_guarded_headers_separate(self):
        seen = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append(dict(self.headers)); self.send_response(200); self.send_header("Content-Type","application/json"); self.send_header("Content-Length","2"); self.end_headers(); self.wfile.write(b"{}")
            def log_message(self, *args): pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler); threading.Thread(target=server.serve_forever, daemon=True).start(); self.addCleanup(lambda: (server.shutdown(), server.server_close()))
        client = cli.local.Client(token="a"*64, mode="plugin", port=server.server_address[1])
        client.post_json(f"/api/machines/{DEVICE}/workspace/launch-preview", {"x": 1})
        self.assertEqual(seen[0]["X-Herdr-Launcher"], "1"); self.assertEqual(seen[0]["X-Herdr-Machine"], "1")
        self.assertNotIn("X-Herdr-Machine-Mutation", seen[0])
        with self.assertRaisesRegex(cli.local.TransportError, "http_status"):
            client.post_json(f"/api/machines/{DEVICE}/workspace/launch", {"request_id": "33333333-3333-4333-8333-333333333333"})
        self.assertEqual(seen[1]["X-Herdr-Launcher"], "1"); self.assertEqual(seen[1]["X-Herdr-Machine"], "1")

    def test_main_launch_error_projection_is_closed_for_local_and_remote_preview(self):
        responses = []
        catalog = {"available": True, "revision": "r1", "profiles": [{"id": "sample", "label": "Sample", "directory": "/SECRET", "runtimes": [{"id": "codex", "label": "Codex", "presets": [{"id": "base", "label": "Base", "model": "m", "effort": "e"}]}]}]}
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps(catalog).encode(); self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
            def do_POST(self):
                status, value = responses.pop(0); body = value if isinstance(value, bytes) else json.dumps(value).encode()
                self.send_response(status); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
            def log_message(self, *args): pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler); threading.Thread(target=server.serve_forever, daemon=True).start(); self.addCleanup(lambda: (server.shutdown(), server.server_close()))
        known = {"error": {"code": "catalog_changed", "message": "SECRET"}}
        malformed = ((409, {"error": {"code": "unknown_code", "message": "SECRET"}}), (400, known), (409, {"error": {"code": [], "message": "SECRET"}}), (409, {"error": {"code": "catalog_changed", "message": "SECRET", "data": "SECRET"}}), (409, b'{"error":'))
        for device, local_id in ((DEVICE, DEVICE), ("22222222-2222-4222-8222-222222222222", DEVICE)):
            responses.append((409, known)); output = io.StringIO(); client = cli.local.Client(token="a" * 64, mode="plugin", port=server.server_address[1])
            with patch.object(cli.local, "discover", return_value=client), patch.object(cli, "read_identity", return_value={"device_id": local_id}), patch("sys.stdout", output):
                self.assertEqual(cli.main(["launch-preview", "--device", device, "--catalog-revision", "r1", "--profile", "sample", "--runtime", "codex", "--preset", "base"]), 1)
            self.assertEqual(json.loads(output.getvalue()), {"error": {"code": "catalog_changed"}}); self.assertNotIn("SECRET", output.getvalue())
        for status, response in malformed:
            with self.subTest(status=status, response=response):
                responses.append((status, response)); output = io.StringIO(); client = cli.local.Client(token="a" * 64, mode="plugin", port=server.server_address[1])
                with patch.object(cli.local, "discover", return_value=client), patch.object(cli, "read_identity", return_value={"device_id": DEVICE}), patch("sys.stdout", output):
                    self.assertEqual(cli.main(["launch-preview", "--device", DEVICE, "--catalog-revision", "r1", "--profile", "sample", "--runtime", "codex", "--preset", "base"]), 1)
                self.assertEqual(json.loads(output.getvalue()), {"error": {"code": "http_status"}}); self.assertNotIn("SECRET", output.getvalue())
    def test_main_advanced_session_reads_once_and_posts_once(self):
        root = Path(tempfile.mkdtemp(dir="/private/tmp")); root.chmod(0o700); self.addCleanup(lambda: __import__("shutil").rmtree(root))
        session = root / "session"; session.write_text("a" * 64); session.chmod(0o600)
        client = Client(); calls = []
        catalog = {"available": True, "revision": "r1", "profiles": [{"id": "sample", "label": "Sample", "directory": "/SECRET", "runtimes": [{"id": "codex", "label": "Codex", "presets": [{"id": "base", "label": "Base", "model": "m", "effort": "medium"}], "advanced": {"models": ["m"], "efforts": ["high"], "contexts": [], "permissions": [], "approvals": [], "sandboxes": [], "networks": []}}]}]}
        client.request_json = lambda path: catalog
        client.post_json = lambda path, body: (calls.append((path, body)) or {"catalog_revision":"r1","plan_digest":"a"*64,"can_launch":True,"summary":{"runtime":"codex","preset":"base","model":"m","effort":"high","context":"x","action":"resume"},"launcher_args":["SECRET"]})
        output = io.StringIO()
        with patch.object(cli.local, "discover", return_value=client), patch.object(cli, "read_identity", return_value={"device_id": DEVICE}), patch.object(cli.files, "read_private_text", wraps=cli.files.read_private_text) as reader, patch("sys.stdout", output):
            self.assertEqual(cli.main(["launch-preview","--device",DEVICE,"--catalog-revision","r1","--profile","sample","--runtime","codex","--preset","base","--model","m","--effort","high","--session-id-file",str(session),"--session-action","resume"]),0)
        self.assertEqual(reader.call_count, 1); self.assertEqual(len(calls), 1); self.assertNotIn("SECRET", output.getvalue())

    def test_main_invalid_advanced_option_posts_nothing(self):
        client = Client(); client.request_json = lambda path: {"available":True,"revision":"r1","profiles":[{"id":"sample","label":"Sample","directory":"/x","runtimes":[{"id":"codex","label":"C","presets":[{"id":"base","label":"B","model":"m","effort":"e"}],"advanced":{"models":["m"],"efforts":[],"contexts":[],"permissions":[],"approvals":[],"sandboxes":[],"networks":[]}}]}]}
        client.post_json = lambda *args: self.fail("POST")
        with patch.object(cli.local,"discover",return_value=client), patch.object(cli,"read_identity",return_value={"device_id":DEVICE}), patch("sys.stdout",io.StringIO()):
            self.assertEqual(cli.main(["launch-preview","--device",DEVICE,"--catalog-revision","r1","--profile","sample","--runtime","codex","--preset","base","--model","bad"]),1)
    def test_catalog_preserves_unicode_label(self):
        client = Client()
        value = client.request_json("")
        value["profiles"][0]["label"] = "개인"
        client.request_json = lambda _: value
        self.assertEqual(cli.launch.catalog(client, DEVICE, DEVICE)["profiles"][0]["label"], "개인")

    def test_main_catalog_projects_directory_and_uses_exact_remote_path(self):
        client, output = Client(), io.StringIO()
        with patch.object(cli.local, "discover", return_value=client), patch.object(cli, "read_identity", return_value={"device_id": "22222222-2222-4222-8222-222222222222"}), patch("sys.stdout", output):
            self.assertEqual(cli.main(["launch-catalog", "--device", DEVICE]), 0)
        self.assertEqual(client.calls, [f"/api/machines/{DEVICE}/workspace/launch-catalog"])
        self.assertNotIn("SECRET", output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["profiles"][0]["id"], "sample")

    def test_preview_rejects_unknown_selection_and_projects_secret_fields(self):
        client = Client()
        client.request_json = lambda path: {"available": True, "revision": "r1", "profiles": [{"id": "sample", "label": "Sample", "directory": "/SECRET", "runtimes": [{"id": "codex", "label": "Codex", "presets": [{"id": "base", "label": "Base", "model": "m", "effort": "medium"}]}]}]}
        client.post_json = lambda path, body: {"catalog_revision": "r1", "plan_digest": "a" * 64, "can_launch": True, "summary": {"runtime": "codex", "preset": "base", "model": "m", "effort": "medium", "context": "x"}, "launcher_args": ["SECRET"]}
        output = io.StringIO()
        with patch.object(cli.local, "discover", return_value=client), patch.object(cli, "read_identity", return_value={"device_id": DEVICE}), patch("sys.stdout", output):
            self.assertEqual(cli.main(["launch-preview", "--device", DEVICE, "--catalog-revision", "r1", "--profile", "sample", "--runtime", "codex", "--preset", "base"]), 0)
        self.assertNotIn("SECRET", output.getvalue())
        self.assertEqual(client.calls[-1][0] if client.calls else "", "")

    def test_main_unknown_preview_selections_never_post(self):
        catalog = {"available": True, "revision": "r1", "profiles": [{"id": "sample", "label": "Sample", "directory": "/SECRET", "runtimes": [{"id": "codex", "label": "Codex", "presets": [{"id": "base", "label": "Base", "model": "m", "effort": "medium"}]}]}]}
        choices = (("--catalog-revision", "old"), ("--profile", "other"), ("--runtime", "claude"), ("--preset", "other"))
        for flag, replacement in choices:
            with self.subTest(flag=flag):
                client, output, posts = Client(), io.StringIO(), []
                client.request_json = lambda path: catalog
                client.post_json = lambda *args: posts.append(args)
                argv = ["launch-preview", "--device", DEVICE, "--catalog-revision", "r1", "--profile", "sample", "--runtime", "codex", "--preset", "base"]
                argv[argv.index(flag) + 1] = replacement
                with patch.object(cli.local, "discover", return_value=client), patch.object(cli, "read_identity", return_value={"device_id": DEVICE}), patch("sys.stdout", output):
                    self.assertEqual(cli.main(argv), 1)
                self.assertEqual(posts, [])
                self.assertEqual(json.loads(output.getvalue()), {"error": {"code": "invalid_selection"}})
                self.assertNotIn("SECRET", output.getvalue())

    def test_main_rejects_malformed_preview_response_without_secret_projection(self):
        catalog = {"available": True, "revision": "r1", "profiles": [{"id": "sample", "label": "Sample", "directory": "/SECRET", "runtimes": [{"id": "codex", "label": "Codex", "presets": [{"id": "base", "label": "Base", "model": "m", "effort": "medium"}]}]}]}
        valid = {"catalog_revision": "r1", "plan_digest": "a" * 64, "can_launch": True, "summary": {"runtime": "codex", "preset": "base", "model": "m", "effort": "medium", "context": "x"}, "launcher_args": []}
        malformed = []
        malformed.append({**valid, "extra": "SECRET"})
        missing = dict(valid); missing.pop("summary"); malformed.append(missing)
        malformed.extend([{**valid, "plan_digest": "a" * 63}, {**valid, "plan_digest": "g" * 64}, {**valid, "catalog_revision": "other"}, {**valid, "summary": []}, {**valid, "can_launch": "SECRET"}])
        for response in malformed:
            with self.subTest(response=response):
                client, output, posts = Client(), io.StringIO(), []
                client.request_json = lambda path: catalog
                client.post_json = lambda *args: (posts.append(args) or response)
                with patch.object(cli.local, "discover", return_value=client), patch.object(cli, "read_identity", return_value={"device_id": DEVICE}), patch("sys.stdout", output):
                    self.assertEqual(cli.main(["launch-preview", "--device", DEVICE, "--catalog-revision", "r1", "--profile", "sample", "--runtime", "codex", "--preset", "base"]), 1)
                self.assertEqual(len(posts), 1)
                self.assertEqual(json.loads(output.getvalue()), {"error": {"code": "invalid_response"}})
                self.assertNotIn("SECRET", output.getvalue())

    def test_main_preview_requires_hidden_launcher_args_and_requested_summary(self):
        root = Path(tempfile.mkdtemp(dir="/private/tmp")); root.chmod(0o700); self.addCleanup(lambda: __import__("shutil").rmtree(root))
        session = root / "session"; session.write_text("a" * 64); session.chmod(0o600)
        catalog = {"available": True, "revision": "r1", "profiles": [{"id": "sample", "label": "Sample", "directory": "/SECRET", "runtimes": [{"id": "codex", "label": "Codex", "presets": [{"id": "base", "label": "Base", "model": "m", "effort": "e"}], "advanced": {"models": ["m"], "efforts": ["e"], "efforts_by_model": {"m": ["e"]}, "contexts": ["한국어"], "permissions": [], "approvals": [], "sandboxes": []}}]}]}
        valid = {"catalog_revision": "r1", "plan_digest": "a" * 64, "can_launch": True, "reason": "SECRET", "summary": {"runtime": "codex", "preset": "base", "model": "m", "effort": "e", "context": "한국어", "action": "resume"}, "launcher_args": ["SECRET"]}
        client, output = Client(), io.StringIO()
        client.request_json = lambda path: catalog; client.post_json = lambda *args: valid
        argv = ["launch-preview", "--device", DEVICE, "--catalog-revision", "r1", "--profile", "sample", "--runtime", "codex", "--preset", "base", "--model", "m", "--effort", "e", "--context", "한국어", "--session-id-file", str(session), "--session-action", "resume"]
        with patch.object(cli.local, "discover", return_value=client), patch.object(cli, "read_identity", return_value={"device_id": DEVICE}), patch("sys.stdout", output):
            self.assertEqual(cli.main(argv), 0)
        self.assertEqual(json.loads(output.getvalue())["summary"]["context"], "한국어")
        self.assertNotIn("SECRET", output.getvalue())
        malformed = []
        missing = dict(valid); missing.pop("launcher_args"); malformed.append(missing)
        malformed.append({**valid, "launcher_args": ["ok", 1]})
        for key, replacement in (("runtime", "claude"), ("preset", "other"), ("model", "other"), ("effort", "other"), ("context", "other"), ("action", "fork")):
            malformed.append({**valid, "summary": {**valid["summary"], key: replacement}})
        for response in malformed:
            with self.subTest(response=response):
                client, output = Client(), io.StringIO()
                client.request_json = lambda path: catalog; client.post_json = lambda *args: response
                with patch.object(cli.local, "discover", return_value=client), patch.object(cli, "read_identity", return_value={"device_id": DEVICE}), patch("sys.stdout", output):
                    self.assertEqual(cli.main(argv), 1)
                self.assertEqual(json.loads(output.getvalue()), {"error": {"code": "invalid_response"}})
                self.assertNotIn("SECRET", output.getvalue())

    def test_main_session_catalog_projects_private_fields_and_fixed_errors(self):
        safe = {"available": True, "revision": "r1", "status": "partial", "reason": "SECRET", "problems": [{"profile": "sample", "code": "secret_problem"}], "items": [{"id": "safe", "profile": "sample", "runtime": "codex", "native_id": "SECRET-native", "launcher_session_id": "SECRET-session", "source_root": "/SECRET", "native_home": "/SECRET/home", "transcript_path": "/SECRET/transcript", "archived": False, "availability": "ready", "can_resume": True, "can_fork": False}]}
        client, output = Client(), io.StringIO()
        client.request_json = lambda path: safe
        with patch.object(cli.local, "discover", return_value=client), patch.object(cli, "read_identity", return_value={"device_id": DEVICE}), patch("sys.stdout", output):
            self.assertEqual(cli.main(["launch-sessions", "--device", DEVICE]), 0)
        self.assertEqual(json.loads(output.getvalue()), {"device_id": DEVICE, "available": True, "revision": "r1", "status": "partial", "items": [{"id": "safe", "profile": "sample", "runtime": "codex", "availability": "ready", "can_resume": True, "can_fork": False}]})
        self.assertNotIn("SECRET", output.getvalue())
        for status in ("ready", "unavailable"):
            with self.subTest(status=status):
                client, output = Client(), io.StringIO()
                client.request_json = lambda path: {**safe, "status": status, "items": []}
                with patch.object(cli.local, "discover", return_value=client), patch.object(cli, "read_identity", return_value={"device_id": DEVICE}), patch("sys.stdout", output):
                    self.assertEqual(cli.main(["launch-sessions", "--device", DEVICE]), 0)
                self.assertEqual(json.loads(output.getvalue())["status"], status)
        malformed = (
            {"available": False, "revision": "r1", "status": "SECRET", "items": []},
            {"available": False, "revision": "r1", "status": [], "items": []},
            {"available": True, "revision": "r1", "items": [{**safe["items"][0], "path": "/SECRET/unknown"}]},
            {"available": True, "revision": "r1", "items": [{**safe["items"][0], "availability": "SECRET"}]},
            {"available": True, "revision": "r1", "items": [{**safe["items"][0], "runtime": []}]},
            {"available": True, "revision": "r1", "items": [{key: value for key, value in safe["items"][0].items() if key != "native_id"}]},
            {"available": True, "revision": "r1", "items": [{**safe["items"][0], "launcher_session_id": []}]},
            {"available": True, "revision": "r1", "items": [safe["items"][0], dict(safe["items"][0])]},
        )
        for value in malformed:
            with self.subTest(value=value):
                client, output = Client(), io.StringIO()
                client.request_json = lambda path: value
                with patch.object(cli.local, "discover", return_value=client), patch.object(cli, "read_identity", return_value={"device_id": DEVICE}), patch("sys.stdout", output):
                    self.assertEqual(cli.main(["launch-sessions", "--device", DEVICE]), 1)
                self.assertEqual(json.loads(output.getvalue()), {"error": {"code": "invalid_response"}})
                self.assertNotIn("SECRET", output.getvalue())

    def test_main_catalog_accepts_helper_efforts_by_model_and_fences_effort(self):
        catalog = {"available": True, "revision": "r1", "profiles": [{"id": "sample", "label": "Sample", "directory": "/SECRET", "runtimes": [{"id": "codex", "label": "Codex", "presets": [{"id": "base", "label": "Base", "model": "m1", "effort": "e1"}], "advanced": {"models": ["m1", "m2"], "efforts": ["e1", "e2"], "efforts_by_model": {"m1": ["e1"], "m2": ["e2"]}, "contexts": [], "permissions": [], "approvals": [], "sandboxes": []}}]}]}
        cases = ((["--model", "m2", "--effort", "e2"], 0), (["--model", "m2", "--effort", "e1"], 1), (["--effort", "e2"], 1))
        for extra, expected in cases:
            with self.subTest(extra=extra):
                client, output, posts = Client(), io.StringIO(), []
                client.request_json = lambda path: catalog
                client.post_json = lambda *args: (posts.append(args) or {"catalog_revision": "r1", "plan_digest": "a" * 64, "can_launch": True, "summary": {"runtime": "codex", "preset": "base", "model": "m2", "effort": "e2", "context": "x"}, "launcher_args": []})
                with patch.object(cli.local, "discover", return_value=client), patch.object(cli, "read_identity", return_value={"device_id": DEVICE}), patch("sys.stdout", output):
                    self.assertEqual(cli.main(["launch-preview", "--device", DEVICE, "--catalog-revision", "r1", "--profile", "sample", "--runtime", "codex", "--preset", "base", *extra]), expected)
                self.assertEqual(len(posts), expected == 0)
                self.assertNotIn("SECRET", output.getvalue())

    def test_main_rejects_null_efforts_by_model_before_preview_post(self):
        client, output, posts = Client(), io.StringIO(), []
        client.request_json = lambda path: {"available": True, "revision": "r1", "profiles": [{"id": "sample", "label": "Sample", "directory": "/SECRET", "runtimes": [{"id": "codex", "label": "Codex", "presets": [{"id": "base", "label": "Base", "model": "m", "effort": "e"}], "advanced": {"models": ["m"], "efforts": ["e"], "efforts_by_model": None, "contexts": [], "permissions": [], "approvals": [], "sandboxes": []}}]}]}
        client.post_json = lambda *args: posts.append(args)
        with patch.object(cli.local, "discover", return_value=client), patch.object(cli, "read_identity", return_value={"device_id": DEVICE}), patch("sys.stdout", output):
            self.assertEqual(cli.main(["launch-preview", "--device", DEVICE, "--catalog-revision", "r1", "--profile", "sample", "--runtime", "codex", "--preset", "base", "--effort", "e"]), 1)
        self.assertEqual(posts, [])
        self.assertEqual(json.loads(output.getvalue()), {"error": {"code": "invalid_response"}})
        self.assertNotIn("SECRET", output.getvalue())

    def test_main_catalog_rejects_non_string_runtime_enum_before_membership(self):
        client, output = Client(), io.StringIO()
        client.request_json = lambda path: {"available": True, "revision": "r1", "profiles": [{"id": "sample", "label": "Sample", "directory": "/SECRET", "runtimes": [{"id": [], "label": "Codex", "presets": []}]}]}
        with patch.object(cli.local, "discover", return_value=client), patch.object(cli, "read_identity", return_value={"device_id": DEVICE}), patch("sys.stdout", output):
            self.assertEqual(cli.main(["launch-catalog", "--device", DEVICE]), 1)
        self.assertEqual(json.loads(output.getvalue()), {"error": {"code": "invalid_response"}})
        self.assertNotIn("SECRET", output.getvalue())

    def test_main_catalog_rejects_duplicate_profile_runtime_and_preset_ids(self):
        base = {"id": "sample", "label": "Sample", "directory": "/SECRET", "runtimes": [{"id": "codex", "label": "Codex", "presets": [{"id": "base", "label": "Base", "model": "m", "effort": "e"}]}]}
        duplicate_profile = {"available": True, "revision": "r1", "profiles": [base, dict(base)]}
        duplicate_runtime = {"available": True, "revision": "r1", "profiles": [{**base, "runtimes": [base["runtimes"][0], dict(base["runtimes"][0])]}]}
        duplicate_preset = {"available": True, "revision": "r1", "profiles": [{**base, "runtimes": [{**base["runtimes"][0], "presets": [base["runtimes"][0]["presets"][0], dict(base["runtimes"][0]["presets"][0])]}]}]}
        for value in (duplicate_profile, duplicate_runtime, duplicate_preset):
            with self.subTest(value=value):
                client, output = Client(), io.StringIO()
                client.request_json = lambda path: value
                with patch.object(cli.local, "discover", return_value=client), patch.object(cli, "read_identity", return_value={"device_id": DEVICE}), patch("sys.stdout", output):
                    self.assertEqual(cli.main(["launch-catalog", "--device", DEVICE]), 1)
                self.assertEqual(json.loads(output.getvalue()), {"error": {"code": "invalid_response"}})
                self.assertNotIn("SECRET", output.getvalue())


if __name__ == "__main__":
    unittest.main()
