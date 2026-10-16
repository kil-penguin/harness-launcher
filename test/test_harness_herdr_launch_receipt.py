import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "bin"))

import harness_herdr_launch_receipt as receipt


class LaunchReceiptStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        (self.home / ".local/state").mkdir(parents=True)
        for path in (self.home, self.home / ".local", self.home / ".local/state"):
            path.chmod(0o700)
        self.store = receipt.LaunchReceiptStore(home=self.home)
        self.intent = str(uuid.uuid4())
        self.device = str(uuid.uuid4())
        self.request = str(uuid.uuid4())

    def test_create_persists_only_safe_unknown_receipt(self):
        result = self.store.create(self.intent, self.device, self.request)
        self.assertEqual(result, {"intent": self.intent, "device_id": self.device,
                                  "request_id": self.request, "phase": "unknown"})
        path = self.store.path(self.intent)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(path.read_text()), {"version": 1, "intent": self.intent,
                         "device_id": self.device, "request_id": self.request, "phase": "unknown"})

    def test_rejects_duplicate_intent_and_unsafe_phase(self):
        self.store.create(self.intent, self.device, self.request)
        with self.assertRaises(receipt.ReceiptError):
            self.store.create(self.intent, self.device, str(uuid.uuid4()))
        with self.assertRaises(receipt.ReceiptError):
            self.store.update(self.intent, "launch_started")
        with self.assertRaises(receipt.ReceiptError):
            self.store.update(self.intent, [])

    def test_started_then_unknown_keeps_only_the_five_receipt_fields(self):
        self.store.create(self.intent, self.device, self.request)
        started = self.store.update(self.intent, "started", {"workspace_id": "safe", "pane_id": "safe"})
        self.assertEqual(set(started), {"intent", "device_id", "request_id", "phase"})
        unknown = self.store.update(self.intent, "unknown")
        self.assertEqual(unknown["phase"], "unknown")
        self.assertEqual(set(json.loads(self.store.path(self.intent).read_text())), {"version", "intent", "device_id", "request_id", "phase"})

    def test_rejects_malformed_or_private_receipt(self):
        self.store.create(self.intent, self.device, self.request)
        path = self.store.path(self.intent)
        path.write_text('{"version":1,"intent":"x","session_id":"secret"}')
        path.chmod(0o600)
        with self.assertRaises(receipt.ReceiptError):
            self.store.read(self.intent)

    def test_rejects_public_or_symlink_receipt(self):
        self.store.create(self.intent, self.device, self.request)
        path = self.store.path(self.intent)
        path.chmod(0o644)
        with self.assertRaises(receipt.ReceiptError):
            self.store.read(self.intent)
        path.chmod(0o600)
        saved = path.with_name("saved")
        path.rename(saved)
        os.symlink(saved, path)
        with self.assertRaises(receipt.ReceiptError):
            self.store.read(self.intent)

    def test_rejects_replaced_or_public_private_parent(self):
        self.store.create(self.intent, self.device, self.request)
        self.store.root.chmod(0o755)
        with self.assertRaises(receipt.ReceiptError):
            self.store.read(self.intent)
        self.store.root.chmod(0o700)
        displaced = self.store.root.with_name("displaced")
        self.store.root.rename(displaced)
        os.symlink(displaced, self.store.root)
        with self.assertRaises(receipt.ReceiptError):
            self.store.read(self.intent)

    def test_fsync_failure_does_not_leave_receipt(self):
        original = receipt.os.fsync
        receipt.os.fsync = lambda _: (_ for _ in ()).throw(OSError("fsync"))
        try:
            with self.assertRaises(OSError):
                self.store.create(self.intent, self.device, self.request)
        finally:
            receipt.os.fsync = original
        self.assertFalse(self.store.path(self.intent).exists())


if __name__ == "__main__":
    unittest.main()
