"""Projection checks and bounded Native RPC transport, using owned fixtures."""
import importlib.util
import base64
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("codex_history_native", ROOT / "bin/codex_history_native.py")
native = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(native)
ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
ITEM = {"type": "userMessage", "id": "question", "clientId": None,
        "content": [{"type": "text", "text": "fixture sentinel", "text_elements": []}]}


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "codex"; self.home.mkdir()
        sessions = self.home / "sessions"; sessions.mkdir()
        (sessions / ("rollout-" + ID + ".jsonl")).write_text(json.dumps({
            "type": "session_meta", "payload": {"id": ID, "history_mode": "paginated"}}) + "\n" +
            json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user"}}) + "\n")
        db = sqlite3.connect(self.home / "thread_history_1.sqlite")
        db.execute("CREATE TABLE thread_turns(thread_id TEXT, turn_id TEXT, rollout_ordinal INTEGER, status TEXT)")
        db.execute("CREATE TABLE thread_items(thread_id TEXT, turn_id TEXT, item_id TEXT, rollout_ordinal INTEGER, item_json TEXT)")
        db.execute("INSERT INTO thread_turns VALUES (?, 'turn', 1, 'completed')", (ID,))
        db.execute("INSERT INTO thread_items VALUES (?, 'turn', 'question', 2, ?)", (ID, json.dumps(ITEM)))
        db.commit(); db.close()

    def test_projection_requires_nonempty_rows_for_nonempty_transcript(self):
        projection = native.expected_history(self.home)
        self.assertEqual(projection[ID]["turns"], ["turn"])
        self.assertEqual(projection[ID]["items"], [{"turnId": "turn", "item": ITEM}])
        db = sqlite3.connect(self.home / "thread_history_1.sqlite")
        db.execute("DELETE FROM thread_items"); db.commit(); db.close()
        with self.assertRaises(native.NativeVerificationError): native.expected_history(self.home)

    def test_empty_success_and_changed_wire_items_never_verify(self):
        class Rpc:
            def __init__(self, *args): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def call(self, method, params):
                if method == 'fs/readFile': return {'dataBase64':base64.b64encode(Path(params['path']).read_bytes()).decode()}
                if method == "thread/resume": return {"thread": {"id": ID}}
                return {"data": [], "nextCursor": None}
        with patch.object(native, "NativeRpc", Rpc), patch.object(native, "binary_identity", return_value={"version": "0.161.0", "sha256": "f" * 64}):
            with self.assertRaises(native.NativeVerificationError): native.verify_native(self.home, {}, "/fixture/codex")

    def test_all_pages_match_preserved_projection(self):
        calls = []
        class Rpc:
            def __init__(self, *args): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def call(self, method, params):
                calls.append((method, params))
                if method == 'fs/readFile': return {'dataBase64':base64.b64encode(Path(params['path']).read_bytes()).decode()}
                if method == "thread/resume": return {"thread": {"id": ID}}
                if method == "thread/turns/list": return {"data": [{"id": "turn"}], "nextCursor": None}
                if not params.get("cursor"): return {"data": [], "nextCursor": "second"}
                return {"data": [{"turnId": "turn", "item": ITEM}], "nextCursor": None}
        with patch.object(native, "NativeRpc", Rpc), patch.object(native, "binary_identity", return_value={"version": "0.161.0", "sha256": "f" * 64}):
            receipt = native.verify_native(self.home, {}, "/fixture/codex")
        self.assertEqual(receipt["threads"][0]["item_count"], 1)
        self.assertEqual(receipt["threads"][0]["turn_count"], 1)
        self.assertEqual(receipt["model_requests"], 0)
        self.assertEqual([method for method,_ in calls if method!='fs/readFile'][0], "thread/resume")
        self.assertTrue(any(p.get("cursor") == "second" for _, p in calls))

    def test_parent_reference_must_be_inside_archive(self):
        path = next((self.home / "sessions").iterdir())
        rows = path.read_text().splitlines(); meta = json.loads(rows[0])
        meta["payload"]["history_base"] = {"thread_id": "ffffffff-1111-4222-8333-444444444444"}
        path.write_text(json.dumps(meta) + "\n" + "\n".join(rows[1:]) + "\n")
        with self.assertRaises(native.NativeVerificationError): native.expected_history(self.home)

    def test_orphan_segment_cannot_be_ignored_by_successful_other_thread(self):
        orphan='ffffffff-1111-4222-8333-444444444444'
        (self.home/'sessions'/('rollout-'+orphan+'_0001.jsonl')).write_text(
            json.dumps({'type':'response_item','payload':{'type':'message','role':'user'}})+'\n')
        with self.assertRaises(native.NativeVerificationError): native.expected_history(self.home)

    def test_fork_inherits_parent_projection_only_at_matching_byte_ordinal_cut(self):
        parent=next((self.home/'sessions').iterdir())
        rows=[json.loads(line) for line in parent.read_text().splitlines()]
        rows[0]['ordinal']=0;rows[1]['ordinal']=3
        parent.write_text(''.join(json.dumps(row)+'\n' for row in rows))
        child='01a123ce-3880-7613-81c9-79cd5213dcec'
        meta={'ordinal':0,'type':'session_meta','payload':{'id':child,'history_mode':'paginated',
              'history_base':{'thread_id':ID,'end_ordinal_exclusive':4,'end_byte_offset':parent.stat().st_size}}}
        path=self.home/'sessions'/('rollout-'+child+'.jsonl');path.write_text(json.dumps(meta)+'\n')
        history=native.expected_history(self.home)
        self.assertEqual(history[child],history[ID])
        meta['payload']['history_base']['end_byte_offset']+=1;path.write_text(json.dumps(meta)+'\n')
        with self.assertRaises(native.NativeVerificationError):native.expected_history(self.home)

    def test_projection_cursor_cannot_claim_bytes_beyond_preserved_rollout(self):
        db=sqlite3.connect(self.home/'thread_history_1.sqlite')
        db.execute('CREATE TABLE thread_history_projection_state(thread_id TEXT,next_rollout_byte_offset INTEGER)')
        db.execute('INSERT INTO thread_history_projection_state VALUES (?,?)',(ID,100000))
        db.commit();db.close()
        with self.assertRaises(native.NativeVerificationError):native.expected_history(self.home)

    def test_projection_thread_without_rollout_is_not_ignored(self):
        db=sqlite3.connect(self.home/'thread_history_1.sqlite')
        db.execute("INSERT INTO thread_turns VALUES ('unpreserved','turn',1,'completed')")
        db.commit();db.close()
        with self.assertRaises(native.NativeVerificationError):native.expected_history(self.home)

    def test_only_characterized_native_versions_verify(self):
        binary = self.home.parent / "codex-binary"; binary.write_text("fixture"); binary.chmod(0o755)
        for version, accepted in (("0.161.0", True), ("0.162.1", True), ("0.162.0", False), ("0.163.0", False)):
            done = type("Done", (), {"returncode": 0, "stdout": ("codex-cli " + version + "\n").encode()})()
            with patch.object(native, "sandbox_command", return_value=["fixture"]), \
                    patch.object(native.subprocess, "run", return_value=done):
                if accepted:
                    self.assertEqual(native.binary_identity(binary)["version"], version)
                else:
                    with self.assertRaisesRegex(native.NativeVerificationError, "lacks characterized"):
                        native.binary_identity(binary)


if __name__ == "__main__": unittest.main()
