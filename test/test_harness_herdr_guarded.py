#!/usr/bin/env python3
"""Guarded CLI uses a durable JSONL receipt before its first mutation POST."""
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "bin"))
import harness_herdr_guarded as guarded
import harness_herdr_store as store
import harness_herdr_cli as cli
from unittest.mock import patch


DEVICE = "11111111-1111-4111-8111-111111111111"


class Client:
    def __init__(self):
        self.calls = []

    def post_json(self, path, body):
        self.calls.append((path, body))
        if path.endswith("/input"):
            return {"operation_id": "22222222-2222-4222-8222-222222222222",
                    "server_epoch": "epoch", "state": "completed",
                    "effect": {"text_bytes_accepted": 7, "enter_accepted": False}}
        return {"operation_id": "22222222-2222-4222-8222-222222222222",
                "admission_ticket": "dGlja2V0", "server_epoch": "epoch",
                "deadline_monotonic_ms": 1}


class GuardedInputTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/private/tmp"); self.addCleanup(self.temp.cleanup)
        self.root = Path(os.path.realpath(self.temp.name)); self.root.chmod(0o700)
        self.home = self.root / "home"; (self.home / ".local/state").mkdir(parents=True)
        for path in (self.home, self.home / ".local", self.home / ".local/state"):
            path.chmod(0o700)
        self.private = self.root / "private"; self.private.mkdir(); self.private.chmod(0o700)
        self.target = self.private / "target.json"; self.payload = self.private / "payload.txt"
        target = {"device_id": DEVICE, "target": {"server_epoch": "epoch", "workspace_id": "ws",
                  "tab_id": "tab", "pane_id": "pane", "terminal_id": "term", "identity_generation": 0,
                  "agent_binding": {"detected_agent": None, "hook": None, "session": None}}}
        self.target.write_text(json.dumps(target)); self.target.chmod(0o600)
        self.payload.write_text("literal"); self.payload.chmod(0o600)

    def test_durable_receipt_is_flushed_before_prepare_post(self):
        client, output = Client(), io.StringIO()
        guarded.run_input(client, store.Store(home=self.home), DEVICE, str(self.target), str(self.payload),
                          "text", output)
        self.assertEqual(client.calls[0][0], "/api/cli/v1/guarded/prepare")
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["state"], "intent_durable")
        self.assertEqual(json.loads(lines[1])["state"], "completed")

    def test_guard_target_writes_private_wrapper_without_stdout_value(self):
        output = self.private / "sealed-target.json"
        target = json.loads(self.target.read_text())["target"]
        guarded.write_target(str(output), DEVICE, target)
        self.assertEqual(json.loads(output.read_text())["device_id"], DEVICE)
        self.assertEqual(oct(output.stat().st_mode & 0o777), "0o600")

    def test_receipt_flush_failure_stops_before_prepare_post(self):
        class Broken(io.StringIO):
            def flush(self): raise BrokenPipeError()
        client = Client()
        with self.assertRaises(BrokenPipeError):
            guarded.run_input(client, store.Store(home=self.home), DEVICE, str(self.target), str(self.payload),
                              "text", Broken())
        self.assertEqual(client.calls, [])

    def test_lost_prepare_is_durable_unknown_without_a_retry(self):
        class Lost(Client):
            def post_json(self, path, body):
                self.calls.append((path, body)); raise OSError("lost")
        client, output, private_store = Lost(), io.StringIO(), store.Store(home=self.home)
        with self.assertRaises(OSError):
            guarded.run_input(client, private_store, DEVICE, str(self.target), str(self.payload), "text", output)
        intent = json.loads(output.getvalue().splitlines()[0])["intent"]
        self.assertEqual(private_store.read(intent)["state"], "prepare_unknown")
        self.assertEqual(len(client.calls), 1)

    def test_queued_result_is_safe_but_does_not_become_terminal(self):
        client, output = Client(), io.StringIO()
        client.post_json = lambda path, body: ({"operation_id": "22222222-2222-4222-8222-222222222222", "admission_ticket": "dGlja2V0", "server_epoch": "epoch", "deadline_monotonic_ms": 1} if path.endswith("prepare") else {"operation_id": "22222222-2222-4222-8222-222222222222", "server_epoch": "epoch", "state": "queued", "effect": {"text_bytes_accepted": 0, "enter_accepted": False}})
        guarded.run_input(client, store.Store(home=self.home), DEVICE, str(self.target), str(self.payload), "text", output)
        intent = json.loads(output.getvalue().splitlines()[0])["intent"]
        self.assertEqual(store.Store(home=self.home).read(intent)["state"], "dispatch_started")

    def test_main_emits_only_two_safe_jsonl_rows(self):
        output, client = io.StringIO(), Client()
        with patch.object(cli.local, "discover", return_value=client), patch.object(cli, "read_identity", return_value={"device_id": DEVICE}), patch.object(cli.store, "Store", return_value=store.Store(home=self.home)), patch("sys.stdout", output):
            self.assertEqual(cli.main(["input", "--device", DEVICE, "--target-file", str(self.target), "--text-file", str(self.payload), "--new-operation"]), 0)
        self.assertEqual(len(output.getvalue().splitlines()), 2)
        self.assertNotIn("dGlja2V0", output.getvalue())

    def test_main_agent_prompt_uses_agent_prepare_and_dispatch_without_prompt_leak(self):
        target = json.loads(self.target.read_text())
        target["target"]["agent_binding"] = {
            "detected_agent": "codex",
            "hook": {"source": "herdr:codex", "agent_label": "codex", "session_ref": {"kind": "id", "value": "hook-session"}},
            "session": {"source": "herdr:codex", "agent": "codex", "session_ref": {"kind": "id", "value": "session"}},
        }
        self.target.write_text(json.dumps(target)); self.target.chmod(0o600)
        self.payload.write_text("literal\n\t\r\x1b"); self.payload.chmod(0o600)
        class AgentClient(Client):
            def post_json(self, path, body):
                self.calls.append((path, body))
                if path.endswith("prepare"):
                    return {"operation_id": "22222222-2222-4222-8222-222222222222", "admission_ticket": "dGlja2V0", "server_epoch": "epoch", "deadline_monotonic_ms": 1}
                return {"operation_id": "22222222-2222-4222-8222-222222222222", "server_epoch": "epoch", "state": "completed", "effect": {"text_bytes_accepted": 11, "enter_accepted": True}}
        client, output = AgentClient(), io.StringIO()
        with patch.object(cli.local, "discover", return_value=client), patch.object(cli, "read_identity", return_value={"device_id": DEVICE}), patch.object(cli.store, "Store", return_value=store.Store(home=self.home)), patch("sys.stdout", output):
            self.assertEqual(cli.main(["agent-prompt", "--device", DEVICE, "--target-file", str(self.target), "--text-file", str(self.payload), "--new-operation"]), 0)
        self.assertEqual([path for path, _ in client.calls], ["/api/cli/v1/agent/guarded/prepare", "/api/cli/v1/agent/guarded/prompt"])
        self.assertEqual(client.calls[0][1]["method"], "agent.guarded_prompt.v1")
        self.assertEqual(client.calls[1][1]["prompt"], "literal\n\t\r\x1b")
        self.assertEqual(len(output.getvalue().splitlines()), 2)
        self.assertNotIn("literal", output.getvalue()); self.assertNotIn("session", output.getvalue())

    def test_agent_prompt_loss_after_prepare_keeps_dispatch_started_without_replay(self):
        target = json.loads(self.target.read_text())
        target["target"]["agent_binding"] = {
            "detected_agent": "claude",
            "hook": {"source": "herdr:claude", "agent_label": "claude", "session_ref": {"kind": "id", "value": "hook"}},
            "session": {"source": "herdr:claude", "agent": "claude", "session_ref": {"kind": "id", "value": "session"}},
        }
        self.target.write_text(json.dumps(target)); self.target.chmod(0o600)
        class Lost(Client):
            def post_json(self, path, body):
                self.calls.append((path, body))
                if path.endswith("prepare"):
                    return {"operation_id": "22222222-2222-4222-8222-222222222222", "admission_ticket": "dGlja2V0", "server_epoch": "epoch", "deadline_monotonic_ms": 1}
                raise OSError("lost")
        client, output, private = Lost(), io.StringIO(), store.Store(home=self.home)
        with self.assertRaises(OSError):
            guarded.run_agent_prompt(client, private, DEVICE, str(self.target), str(self.payload), output)
        intent = json.loads(output.getvalue().splitlines()[0])["intent"]
        self.assertEqual(private.read(intent)["state"], "dispatch_started")
        self.assertEqual([path for path, _ in client.calls], ["/api/cli/v1/agent/guarded/prepare", "/api/cli/v1/agent/guarded/prompt"])

    def test_agent_prompt_prepare_loss_is_unknown_after_one_post(self):
        target = json.loads(self.target.read_text())
        target["target"]["agent_binding"] = {
            "detected_agent": "claude",
            "hook": {"source": "herdr:claude", "agent_label": "claude", "session_ref": {"kind": "id", "value": "hook"}},
            "session": {"source": "herdr:claude", "agent": "claude", "session_ref": {"kind": "id", "value": "session"}},
        }
        self.target.write_text(json.dumps(target)); self.target.chmod(0o600)
        class Lost(Client):
            def post_json(self, path, body):
                self.calls.append((path, body)); raise OSError("lost")
        client, output, private = Lost(), io.StringIO(), store.Store(home=self.home)
        with self.assertRaises(OSError):
            guarded.run_agent_prompt(client, private, DEVICE, str(self.target), str(self.payload), output)
        intent = json.loads(output.getvalue().splitlines()[0])["intent"]
        self.assertEqual(private.read(intent)["state"], "prepare_unknown")
        self.assertEqual([path for path, _ in client.calls], ["/api/cli/v1/agent/guarded/prepare"])

    def test_agent_prompt_requires_nonnull_matching_official_binding_before_post(self):
        client, output = Client(), io.StringIO()
        with self.assertRaisesRegex(guarded.GuardedError, "^invalid_guarded_request$"):
            guarded.run_agent_prompt(client, store.Store(home=self.home), DEVICE, str(self.target), str(self.payload), output)
        self.assertEqual(client.calls, [])

    def test_agent_prompt_reads_target_and_text_once_and_shared_status_uses_no_prompt(self):
        target = json.loads(self.target.read_text())
        target["target"]["agent_binding"] = {
            "detected_agent": "codex",
            "hook": {"source": "herdr:codex", "agent_label": "codex", "session_ref": {"kind": "id", "value": "hook"}},
            "session": {"source": "herdr:codex", "agent": "codex", "session_ref": {"kind": "id", "value": "session"}},
        }
        self.target.write_text(json.dumps(target)); self.target.chmod(0o600)
        counts = {"target": 0, "text": 0}
        original_target, original_text = guarded.files.read_private_json_object, guarded.files.read_private_text
        def read_target(*args):
            counts["target"] += 1; return original_target(*args)
        def read_text(*args):
            if args[0] == str(self.payload): counts["text"] += 1
            return original_text(*args)
        client, output, private = Client(), io.StringIO(), store.Store(home=self.home)
        client.post_json = lambda path, body, client=client: (client.calls.append((path, body)) or ({"operation_id": "22222222-2222-4222-8222-222222222222", "admission_ticket": "dGlja2V0", "server_epoch": "epoch", "deadline_monotonic_ms": 1} if path.endswith("prepare") else {"operation_id": "22222222-2222-4222-8222-222222222222", "server_epoch": "epoch", "state": "queued", "effect": {"text_bytes_accepted": 0, "enter_accepted": False}}))
        with patch.object(guarded.files, "read_private_json_object", read_target), patch.object(guarded.files, "read_private_text", read_text):
            guarded.run_agent_prompt(client, private, DEVICE, str(self.target), str(self.payload), output)
        self.assertEqual(counts, {"target": 1, "text": 1})
        intent = json.loads(output.getvalue().splitlines()[0])["intent"]
        output = io.StringIO()
        client.post_json = lambda path, body: (client.calls.append((path, body)) or {"operation_id": "22222222-2222-4222-8222-222222222222", "server_epoch": "epoch", "state": "completed", "effect": {"text_bytes_accepted": 0, "enter_accepted": False}})
        guarded.operation(client, private, intent, output, local_device=DEVICE)
        self.assertEqual(client.calls[-1][1], {"operation_id": "22222222-2222-4222-8222-222222222222", "server_epoch": "epoch"})
        self.assertNotIn("literal", output.getvalue())

    def test_nonnull_binding_and_u63_boundary_are_accepted(self):
        target = json.loads(self.target.read_text())["target"]
        target["identity_generation"] = (1 << 63) - 1
        target["agent_binding"] = {"detected_agent": "codex", "hook": {"source": "hook", "agent_label": "codex", "session_ref": {"kind": "id", "value": "x"}}, "session": {"source": "native", "agent": "codex", "session_ref": {"kind": "path", "value": "/x"}}}
        guarded._target(target)
        target["identity_generation"] = 1 << 63
        with self.assertRaises(guarded.GuardedError): guarded._target(target)

    def test_session_path_requires_absolute_and_rejects_del_c1(self):
        target = json.loads(self.target.read_text())["target"]
        target["agent_binding"]["session"] = {"source": "native", "agent": "codex", "session_ref": {"kind": "path", "value": "relative"}}
        with self.assertRaises(guarded.GuardedError): guarded._target(target)
        target["agent_binding"]["session"]["session_ref"]["value"] = "/bad\x7f"
        with self.assertRaises(guarded.GuardedError): guarded._target(target)

    def test_only_last_four_wire_states_terminalize(self):
        for state in ("prepared", "queued", "writing_text", "waiting_enter", "writing_enter", "completed", "cancelled", "partial_unknown", "rejected"):
            client, output, private = Client(), io.StringIO(), store.Store(home=self.home)
            client.post_json = lambda path, body, state=state, client=client: (client.calls.append((path, body)) or ({"operation_id":"22222222-2222-4222-8222-222222222222","admission_ticket":"dGlja2V0","server_epoch":"epoch","deadline_monotonic_ms":1} if path.endswith("prepare") else {"operation_id":"22222222-2222-4222-8222-222222222222","server_epoch":"epoch","state":state,"effect":{"text_bytes_accepted":0,"enter_accepted":False}}))
            guarded.run_input(client, private, DEVICE, str(self.target), str(self.payload), "text", output)
            intent = json.loads(output.getvalue().splitlines()[0])["intent"]
            self.assertEqual(private.read(intent)["state"], "observed_terminal" if state in {"completed","cancelled","partial_unknown","rejected"} else "dispatch_started")

    def test_malformed_effect_or_secret_reason_does_not_terminalize(self):
        for bad in ({}, {"state": ["SECRET"], "effect": {"text_bytes_accepted": 0, "enter_accepted": False}}, {"state": {"SECRET": 1}, "effect": {"text_bytes_accepted": 0, "enter_accepted": False}}, {"state": "SECRET", "effect": {"text_bytes_accepted": 0, "enter_accepted": False}}, {"effect": {"text_bytes_accepted": 0, "enter_accepted": False}, "reason": ["SECRET"]}, {"effect": {"text_bytes_accepted": 0, "enter_accepted": False}, "reason": {"SECRET": 1}}, {"operation_id": "33333333-3333-4333-8333-333333333333", "effect": {"text_bytes_accepted": 0, "enter_accepted": False}}, {"server_epoch": "SECRET", "effect": {"text_bytes_accepted": 0, "enter_accepted": False}}, {"effect": {"text_bytes_accepted": True, "enter_accepted": False}}, {"effect": {"text_bytes_accepted": -1, "enter_accepted": False}}, {"effect": {"text_bytes_accepted": 1 << 64, "enter_accepted": False}}, {"effect": {"text_bytes_accepted": 0, "enter_accepted": "SECRET"}}, {"effect": {"text_bytes_accepted": 0, "enter_accepted": False}, "reason": "SECRET"}, {"effect": {"text_bytes_accepted": 0, "enter_accepted": False}, "extra": "x"}):
            client, output = Client(), io.StringIO()
            def post(path, body, bad=bad):
                if path.endswith("prepare"): return {"operation_id": "22222222-2222-4222-8222-222222222222", "admission_ticket": "dGlja2V0", "server_epoch": "epoch", "deadline_monotonic_ms": (1 << 64) - 1}
                return {"operation_id": "22222222-2222-4222-8222-222222222222", "server_epoch": "epoch", "state": "completed", **bad}
            client.post_json = post; private_store = store.Store(home=self.home)
            with self.assertRaises(guarded.GuardedError): guarded.run_input(client, private_store, DEVICE, str(self.target), str(self.payload), "text", output)
            intent = json.loads(output.getvalue().splitlines()[0])["intent"]
            self.assertEqual(private_store.read(intent)["state"], "dispatch_started")
            self.assertNotIn("SECRET", output.getvalue())

    def test_wire_reason_state_pairs_and_zero_effect_phases(self):
        zero = {"text_bytes_accepted": 0, "enter_accepted": False}
        positive = {"text_bytes_accepted": 1, "enter_accepted": False}
        def invoke(state, effect, reason=None):
            client, output, private = Client(), io.StringIO(), store.Store(home=self.home)
            def post(path, body):
                if path.endswith("prepare"):
                    return {"operation_id": "22222222-2222-4222-8222-222222222222", "admission_ticket": "dGlja2V0", "server_epoch": "epoch", "deadline_monotonic_ms": 1}
                result = {"operation_id": "22222222-2222-4222-8222-222222222222", "server_epoch": "epoch", "state": state, "effect": effect}
                if reason is not None: result["reason"] = reason
                return result
            client.post_json = post
            return client, output, private
        valid = (("prepared", zero, None), ("queued", zero, None),
                 ("writing_text", positive, None), ("waiting_enter", positive, None),
                 ("writing_enter", positive, None), ("completed", positive, None),
                 ("rejected", zero, "guard_mismatch"), ("rejected", zero, "deadline_exceeded"),
                 ("rejected", zero, "pty_closed"), ("rejected", zero, "io_error"),
                 ("partial_unknown", positive, "guard_mismatch"), ("partial_unknown", positive, "deadline_exceeded"),
                 ("partial_unknown", positive, "pty_closed"), ("partial_unknown", positive, "io_error"),
                 ("cancelled", zero, "cancelled"), ("partial_unknown", positive, "partial_unknown"))
        for state, effect, reason in valid:
            client, output, private = invoke(state, effect, reason)
            guarded.run_input(client, private, DEVICE, str(self.target), str(self.payload), "text", output)
            intent = json.loads(output.getvalue().splitlines()[0])["intent"]
            self.assertEqual(private.read(intent)["state"], "observed_terminal" if state in {"completed", "cancelled", "partial_unknown", "rejected"} else "dispatch_started")
        bad = (("completed", zero, "guard_mismatch"), ("rejected", zero, "cancelled"),
               ("partial_unknown", positive, "cancelled"), ("cancelled", zero, "partial_unknown"),
               ("prepared", positive, None), ("queued", positive, None),
               ("cancelled", positive, "cancelled"), ("cancelled", {"text_bytes_accepted": 0, "enter_accepted": True}, "cancelled"),
               ("rejected", positive, "io_error"), ("rejected", {"text_bytes_accepted": 0, "enter_accepted": True}, "io_error"),
               ("rejected", zero, "SECRET"), ("rejected", zero, ["io_error"]), ("rejected", zero, {"reason": "io_error"}))
        for state, effect, reason in bad:
            client, output, private = invoke(state, effect, reason)
            with self.assertRaisesRegex(guarded.GuardedError, "^invalid_guarded_request$"):
                guarded.run_input(client, private, DEVICE, str(self.target), str(self.payload), "text", output)
            intent = json.loads(output.getvalue().splitlines()[0])["intent"]
            self.assertEqual(private.read(intent)["state"], "dispatch_started")
            self.assertNotIn("SECRET", output.getvalue())

    def test_prepare_deadline_overflow_is_unknown_after_one_post(self):
        class Overflow(Client):
            def post_json(self, path, body):
                self.calls.append((path, body)); return {"operation_id": "22222222-2222-4222-8222-222222222222", "admission_ticket": "dGlja2V0", "server_epoch": "epoch", "deadline_monotonic_ms": 1 << 64}
        client, output, private_store = Overflow(), io.StringIO(), store.Store(home=self.home)
        with self.assertRaises(guarded.GuardedError): guarded.run_input(client, private_store, DEVICE, str(self.target), str(self.payload), "text", output)
        intent = json.loads(output.getvalue().splitlines()[0])["intent"]
        self.assertEqual(private_store.read(intent)["state"], "prepare_unknown")
        self.assertEqual(len(client.calls), 1)

    def test_flush_happens_before_prepare_post(self):
        events = []
        class Stream(io.StringIO):
            def write(self, value): events.append("write"); return super().write(value)
            def flush(self): events.append("flush")
        class Ordered(Client):
            def post_json(self, path, body): events.append("prepare-post" if path.endswith("prepare") else "dispatch-post"); return super().post_json(path, body)
        guarded.run_input(Ordered(), store.Store(home=self.home), DEVICE, str(self.target), str(self.payload), "text", Stream())
        self.assertLess(events.index("flush"), events.index("prepare-post"))

    def test_prepare_transition_conflict_posts_nothing(self):
        private, client, output = store.Store(home=self.home), Client(), io.StringIO()
        with patch.object(private, "transition", side_effect=store.StoreError("private_store_conflict")):
            with self.assertRaises(store.StoreError): guarded.run_input(client, private, DEVICE, str(self.target), str(self.payload), "text", output)
        self.assertEqual(client.calls, [])

    def test_bind_conflict_never_dispatches(self):
        private, client, output = store.Store(home=self.home), Client(), io.StringIO()
        with patch.object(private, "bind_operation", side_effect=store.StoreError("private_store_conflict")):
            with self.assertRaises(store.StoreError): guarded.run_input(client, private, DEVICE, str(self.target), str(self.payload), "text", output)
        self.assertEqual([path for path, _ in client.calls], ["/api/cli/v1/guarded/prepare"])

    def test_dispatch_transition_conflict_never_dispatches(self):
        private, client, output = store.Store(home=self.home), Client(), io.StringIO(); real = private.transition
        def fail_after_prepare(intent, revision, state):
            if state == "dispatch_started": raise store.StoreError("private_store_conflict")
            return real(intent, revision, state)
        with patch.object(private, "transition", side_effect=fail_after_prepare):
            with self.assertRaises(store.StoreError): guarded.run_input(client, private, DEVICE, str(self.target), str(self.payload), "text", output)
        self.assertEqual([path for path, _ in client.calls], ["/api/cli/v1/guarded/prepare"])

    def test_lost_dispatch_keeps_recoverable_record_without_retry(self):
        class Lost(Client):
            def post_json(self, path, body):
                self.calls.append((path, body))
                if path.endswith("input"): raise OSError("lost")
                return {"operation_id": "22222222-2222-4222-8222-222222222222", "admission_ticket": "dGlja2V0", "server_epoch": "epoch", "deadline_monotonic_ms": 1}
        private, client, output = store.Store(home=self.home), Lost(), io.StringIO()
        with self.assertRaises(OSError): guarded.run_input(client, private, DEVICE, str(self.target), str(self.payload), "text", output)
        intent = json.loads(output.getvalue().splitlines()[0])["intent"]
        self.assertEqual(private.read(intent)["state"], "dispatch_started")
        self.assertEqual(len(client.calls), 2)

    def test_status_and_remote_cancel_use_only_stored_operation(self):
        private = store.Store(home=self.home); intent = "33333333-3333-4333-8333-333333333333"
        record = private.create_intent(intent, DEVICE, guarded.METHOD)
        started = private.transition(intent, record["revision"], "prepare_started")
        bound = private.bind_operation(intent, started["revision"], "epoch", "22222222-2222-4222-8222-222222222222", "dGlja2V0")
        private.transition(intent, bound["revision"], "dispatch_started")
        for cancel, local_id, suffix in ((False, DEVICE, "/guarded/operation"), (True, "44444444-4444-4444-8444-444444444444", "/guarded/operation/cancel")):
            client, output = Client(), io.StringIO()
            client.post_json = lambda path, body, client=client: (client.calls.append((path, body)) or {"operation_id": "22222222-2222-4222-8222-222222222222", "server_epoch": "epoch", "state": "completed", "effect": {"text_bytes_accepted": 0, "enter_accepted": False}})
            guarded.operation(client, private, intent, output, cancel, local_id)
            path, body = client.calls[-1]
            self.assertTrue(path.endswith(suffix)); self.assertEqual(body, {"operation_id": "22222222-2222-4222-8222-222222222222", "server_epoch": "epoch"})
            self.assertEqual(len(output.getvalue().splitlines()), 1); self.assertNotIn("dGlja2V0", output.getvalue())

    def test_target_and_payload_are_read_once_before_posts(self):
        client, output, counts = Client(), io.StringIO(), {"json": 0, "text": 0}
        original_json, original_text = guarded.files.read_private_json_object, guarded.files.read_private_text
        def once_json(*args):
            counts["json"] += 1; value = original_json(*args); self.target.write_text("{}"); self.target.chmod(0o600); return value
        def once_text(*args):
            value = original_text(*args)
            if args[0] == str(self.payload): counts["text"] += 1; self.payload.write_text("changed"); self.payload.chmod(0o600)
            return value
        with patch.object(guarded.files, "read_private_json_object", once_json), patch.object(guarded.files, "read_private_text", once_text):
            guarded.run_input(client, store.Store(home=self.home), DEVICE, str(self.target), str(self.payload), "text", output)
        self.assertEqual(counts, {"json": 1, "text": 1})
        self.assertEqual(client.calls[0][1]["body"]["input"]["text"], "literal")
        self.assertNotIn("term", output.getvalue()); self.assertNotIn("literal", output.getvalue())

    def test_main_maps_private_file_error_without_traceback_or_path(self):
        output = io.StringIO()
        with patch.object(cli.local, "discover", return_value=Client()), patch.object(cli, "read_identity", return_value={"device_id": DEVICE}), patch("sys.stdout", output):
            self.assertEqual(cli.main(["input", "--device", DEVICE, "--target-file", "/no/such/secret", "--text-file", str(self.payload), "--new-operation"]), 1)
        self.assertEqual(json.loads(output.getvalue())["error"]["code"], "guarded_request_failed")
        self.assertNotIn("secret", output.getvalue())

    def test_main_preserves_only_operation_unknown_for_unprepared_status_cancel(self):
        for index, phase in enumerate(("intent_durable", "prepare_unknown")):
            for command in ("status", "cancel"):
                private, client, output = store.Store(home=self.home), Client(), io.StringIO()
                intent = f"33333333-3333-4333-8333-{index * 2 + (command == 'cancel'):012d}"
                record = private.create_intent(intent, DEVICE, guarded.METHOD)
                if phase == "prepare_unknown":
                    started = private.transition(intent, record["revision"], "prepare_started")
                    private.transition(intent, started["revision"], "prepare_unknown")
                with patch.object(cli.local, "discover", return_value=client), patch.object(cli, "read_identity", return_value={"device_id": DEVICE}), patch.object(cli.store, "Store", return_value=private), patch("sys.stdout", output):
                    self.assertEqual(cli.main([command, "--intent", intent]), 1)
                self.assertEqual(json.loads(output.getvalue())["error"]["code"], "operation_unknown")
                self.assertEqual(client.calls, [])
        output = io.StringIO()
        with patch.object(cli.local, "discover", return_value=Client()), patch.object(cli, "read_identity", return_value={"device_id": DEVICE}), patch.object(cli.guarded, "operation", side_effect=guarded.GuardedError("SECRET")), patch("sys.stdout", output):
            self.assertEqual(cli.main(["status", "--intent", "33333333-3333-4333-8333-333333333333"]), 1)
        self.assertEqual(json.loads(output.getvalue())["error"]["code"], "guarded_request_failed")
        self.assertNotIn("SECRET", output.getvalue())

    def test_main_refuses_guard_target_for_a_different_pane_before_write(self):
        output, sealed = io.StringIO(), self.private / "sealed"
        class Wrong(Client):
            def post_json(self, path, body):
                target = json.loads(self.target.read_text())["target"] if False else {"server_epoch":"epoch","workspace_id":"ws","tab_id":"tab","pane_id":"other","terminal_id":"term","identity_generation":0,"agent_binding":{"detected_agent":None,"hook":None,"session":None}}
                return {"target": target}
        with patch.object(cli.local, "discover", return_value=Wrong()), patch.object(cli, "read_identity", return_value={"device_id": DEVICE}), patch("sys.stdout", output):
            self.assertEqual(cli.main(["guard-target", "--device", DEVICE, "--pane", "pane", "--out", str(sealed)]), 1)
        self.assertFalse(sealed.exists()); self.assertNotIn("other", output.getvalue())

    def test_main_preserves_legacy_safe_error_codes(self):
        for error, code in ((cli.CommandError("capability_unsupported"), "capability_unsupported"), (cli.local.TransportError("http_503"), "http_503")):
            output = io.StringIO()
            with patch.object(cli.local, "discover", side_effect=error), patch("sys.stdout", output): self.assertEqual(cli.main(["devices"]), 1)
            self.assertEqual(json.loads(output.getvalue())["error"]["code"], code)


if __name__ == "__main__":
    unittest.main()
