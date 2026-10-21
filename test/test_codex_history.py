#!/usr/bin/env python3
"""Filesystem contract for the native Codex history archive boundary."""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
HISTORY = ROOT / "bin" / "codex-history.py"
SPEC = importlib.util.spec_from_file_location("codex_history", HISTORY)
history_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(history_module)
ISOLATION_ID = "11111111-2222-4333-8444-555555555555"
RUNTIME_REVISION = "a" * 40
NATIVE_THREAD_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
PARENT_THREAD_ID = "ffffffff-1111-4222-8333-444444444444"


def uuid_hex():
    return os.urandom(6).hex()


def digest_tree(root, excluded=()):
    entries = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_file() and not path.is_symlink() and relative not in excluded:
            entries.append((relative, hashlib.sha256(path.read_bytes()).hexdigest()))
    return hashlib.sha256(json.dumps(entries, separators=(",", ":")).encode()).hexdigest()


class CodexHistoryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source_root = self.base / "source-root"
        self.source_root.mkdir()
        self.home = self.source_root / ".harness" / "codex"
        self.catalog = self.base / "profile-local-catalog"
        self.receipt = self.base / "receipt.json"
        self._make_home()

    def _make_home(self):
        if self.home.exists():
            shutil.rmtree(self.home)
        current = self.home / "sessions/2026/10/10"
        archived = self.home / "archived_sessions/2026/10/09"
        current.mkdir(parents=True)
        archived.mkdir(parents=True)
        (self.home / "history.jsonl").write_text(
            json.dumps({"session_id": NATIVE_THREAD_ID, "input": "fixture-only-secret"}) + "\n")
        (self.home / "session_index.jsonl").write_text(
            json.dumps({"id": NATIVE_THREAD_ID, "path": "sessions/2026/10/10/rollout-2026-10-10T01-02-03-" + NATIVE_THREAD_ID + ".jsonl"})
            + "\n" + json.dumps({"id": PARENT_THREAD_ID, "path": "archived_sessions/2026/10/09/rollout-2026-10-09T01-02-03-" + PARENT_THREAD_ID + ".jsonl"}) + "\n")
        (current / ("rollout-2026-10-10T01-02-03-" + NATIVE_THREAD_ID + ".jsonl")).write_text(
            json.dumps({"timestamp": "2026-10-10T01:02:03Z", "type": "session_meta", "payload": {
                "id": NATIVE_THREAD_ID, "cwd": "/fixture"}}) + "\n")
        (current / ("rollout-2026-10-10T01-02-03-" + NATIVE_THREAD_ID + "_0001.jsonl")).write_text('{"type":"response_item","id":"segment"}\n')
        (archived / ("rollout-2026-10-09T01-02-03-" + PARENT_THREAD_ID + ".jsonl")).write_text('{"type":"response_item","id":"parent"}\n')
        for name in ("logs_2", "goals_1", "memories_1", "queue_1", "shell_snapshots", "generated_images", "attachments"):
            if name in {"shell_snapshots", "generated_images", "attachments"}:
                (self.home / name).mkdir()
                (self.home / name / "native-data").write_text(name + "\n")
            else:
                self._make_auxiliary_db(name + ".sqlite")
        self._make_state_db()
        self._make_history_db()
        auth_target = self.base / "fixture-auth.json"
        auth_target.write_text('{"token":"fixture-authority"}\n')
        (self.home / "auth.json").symlink_to(auth_target)
        for name, content in (("config.toml", 'model = "fixture"\n'),
                              ("hooks.json", '{"trust":"fixture"}\n'), (".codex-home-prepare.lock", "lock\n")):
            (self.home / name).write_text(content)
        (self.home / "skills").mkdir()
        (self.home / "skills/SKILL.md").write_text("generated\n")
        (self.home / "plugins/cache").mkdir(parents=True)
        (self.home / "plugins/cache/state").write_text("cache\n")

    def _make_auxiliary_db(self, name):
        con = sqlite3.connect(self.home / name)
        self.addCleanup(con.close)
        con.execute("CREATE TABLE retained (value TEXT)")
        con.execute("INSERT INTO retained VALUES (?)", (name,))
        con.commit()

    def _make_state_db(self):
        con = sqlite3.connect(self.home / "state_5.sqlite")
        self.addCleanup(con.close)
        con.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT)")
        con.execute("CREATE TABLE rollout_migration_skipped_rollouts (id TEXT PRIMARY KEY, rollout_path TEXT)")
        con.execute("INSERT INTO threads VALUES (?, ?)", (NATIVE_THREAD_ID, str(self.home / "sessions/2026/10/10" / ("rollout-2026-10-10T01-02-03-" + NATIVE_THREAD_ID + ".jsonl"))))
        con.execute("INSERT INTO rollout_migration_skipped_rollouts VALUES (?, ?)",
                    (PARENT_THREAD_ID, str(self.home / "archived_sessions/2026/10/09" / ("rollout-2026-10-09T01-02-03-" + PARENT_THREAD_ID + ".jsonl"))))
        con.commit()

    def _make_history_db(self):
        path = self.home / "thread_history_1.sqlite"
        con = sqlite3.connect(path)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE history (id TEXT PRIMARY KEY, value TEXT)")
        con.execute("INSERT INTO history VALUES ('thread-1', 'durable')")
        con.commit()
        self.wal_reader = sqlite3.connect(path)
        self.addCleanup(self.wal_reader.close)
        self.assertTrue((self.home / "thread_history_1.sqlite-wal").exists())
        con.close()

    def run_history(self, *args, expected=0):
        self.assertTrue(HISTORY.is_file(), "codex history helper is missing")
        proc = subprocess.run([sys.executable, str(HISTORY), *args], capture_output=True, text=True, timeout=20)
        self.assertEqual(proc.returncode, expected, proc.stderr)
        return proc

    def snapshot(self, *extra, expected=0):
        return self.run_history(
            "snapshot", "--source", str(self.home), "--catalog", str(self.catalog), "--receipt", str(self.receipt),
            "--source-root", str(self.source_root), "--isolation-id", ISOLATION_ID,
            "--runtime-revision", RUNTIME_REVISION, *extra, expected=expected)

    def test_selected_restore_does_not_clone_other_threads_or_native_databases(self):
        """A selected resume must not materialize the entire canonical HOME."""
        unrelated = self.home / 'sessions/2026/10/10/rollout-2026-10-10T01-02-03-00000000-1111-4222-8333-444444444444.jsonl'
        unrelated.write_text('{"type":"response_item","id":"unrelated"}\n')
        (self.home/'external_agent_session_imports.json').write_text('{"imported_threads":["fixture"]}\n')
        receipt = json.loads(self.snapshot().stdout)
        restored = self.base / 'selected-home'
        self.run_history('restore', '--catalog', str(self.catalog), '--snapshot', receipt['snapshot_id'],
                         '--destination', str(restored), '--receipt', str(self.base/'selected-receipt.json'),
                         '--native-id', NATIVE_THREAD_ID)
        self.assertFalse((restored / unrelated.relative_to(self.home)).exists())
        self.assertFalse(list(restored.glob('*.sqlite*')))
        self.assertFalse((restored/'history.jsonl').exists())
        self.assertFalse((restored/'session_index.jsonl').exists())
        self.assertTrue(list(restored.rglob('*'+NATIVE_THREAD_ID+'*.jsonl')))
        self.assertFalse(list(restored.rglob('*'+PARENT_THREAD_ID+'*.jsonl')))
        self.assertTrue(list(restored.rglob('*'+NATIVE_THREAD_ID+'_0001.jsonl')))
        self.assertEqual((restored/'attachments/native-data').read_text(), 'attachments\n')
        self.assertEqual((restored/'external_agent_session_imports.json').read_text(), '{"imported_threads":["fixture"]}\n')
        self.assertTrue((self.catalog/'snapshots'/receipt['snapshot_id']/'data'/unrelated.relative_to(self.home)).is_file())

    def test_missing_selected_uuid_refuses_before_creating_restore_destination(self):
        receipt = json.loads(self.snapshot().stdout)
        destination = self.base/'absent-native'
        self.run_history('restore', '--catalog', str(self.catalog), '--snapshot', receipt['snapshot_id'],
                         '--destination', str(destination), '--receipt', str(self.base/'missing.json'),
                         '--native-id', '00000000-1111-4222-8333-444444444444', expected=2)
        self.assertFalse(destination.exists())

    def test_selected_restore_refuses_parent_projection_before_creating_destination(self):
        physical='00000000-1111-4222-8333-444444444444'
        parent_physical='12345678-1111-4222-8333-444444444444'
        parent_logical='87654321-1111-4222-8333-444444444444'
        reverted=self.home/'sessions/2026/10/10'/('rollout-2026-10-10T01-02-04-'+NATIVE_THREAD_ID+'_'+physical+'.jsonl')
        reverted.write_text(json.dumps({'type':'session_meta','payload':{'id':NATIVE_THREAD_ID,
            'history_base':{'thread_id':parent_physical}}})+'\n')
        parent=reverted.with_name('rollout-2026-10-10T01-02-05-'+parent_logical+'_'+parent_physical+'.jsonl')
        parent.write_text(json.dumps({'type':'session_meta','payload':{'id':parent_logical}})+'\n')
        with sqlite3.connect(self.home/'state_5.sqlite') as database:
            database.execute('UPDATE threads SET rollout_path=? WHERE id=?',(str(reverted),NATIVE_THREAD_ID))
        receipt=json.loads(self.snapshot().stdout);destination=self.base/'reverted-restore'
        self.run_history('restore','--catalog',str(self.catalog),'--snapshot',receipt['snapshot_id'],
            '--destination',str(destination),'--receipt',str(self.base/'reverted.json'),'--native-id',NATIVE_THREAD_ID,expected=2)
        self.assertFalse(destination.exists())
        self.assertTrue((self.catalog/'snapshots'/receipt['snapshot_id']/'data'/reverted.relative_to(self.home)).is_file())

    def test_snapshot_restore_preserves_native_history_without_authority(self):
        source_before = digest_tree(self.home)
        proc = self.snapshot()
        receipt = json.loads(proc.stdout)
        self.assertRegex(receipt["source_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(receipt["source_home"], str(self.home))
        self.assertEqual(receipt["source_root"], str(self.source_root))
        self.assertEqual(receipt["isolation_id"], ISOLATION_ID)
        self.assertEqual(receipt["runtime_revision"], RUNTIME_REVISION)
        self.assertNotIn("fixture-only-secret", proc.stdout)
        self.assertNotIn("fixture-authority", proc.stdout)
        self.assertEqual(digest_tree(self.home), source_before)
        self.assertEqual(stat.S_IMODE(self.catalog.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.receipt.stat().st_mode), 0o600)

        restored = self.base / "restored-home"
        restored.mkdir()
        (restored / "config.toml").write_text("generated destination\n")
        (restored / "hooks.json").write_text("generated destination\n")
        (restored / "skills").mkdir()
        (restored / "skills/SKILL.md").write_text("generated destination\n")
        restore_receipt = self.base / "restore-receipt.json"
        self.run_history("restore", "--catalog", str(self.catalog), "--snapshot", receipt["snapshot_id"],
                         "--destination", str(restored), "--receipt", str(restore_receipt))
        omitted = {"state_5.sqlite", "thread_history_1.sqlite", "thread_history_1.sqlite-wal", "thread_history_1.sqlite-shm",
                   "auth.json", "config.toml", "hooks.json", "skills/SKILL.md", "plugins/cache/state", ".codex-home-prepare.lock",
                   "logs_2.sqlite", "goals_1.sqlite", "memories_1.sqlite", "queue_1.sqlite"}
        self.assertEqual(digest_tree(restored, omitted), digest_tree(self.home, omitted))
        self.assertTrue((restored / "sessions/2026/10/10" / ("rollout-2026-10-10T01-02-03-" + NATIVE_THREAD_ID + "_0001.jsonl")).is_file())
        self.assertTrue((restored / "archived_sessions/2026/10/09" / ("rollout-2026-10-09T01-02-03-" + PARENT_THREAD_ID + ".jsonl")).is_file())
        self.assertEqual((restored / "config.toml").read_text(), "generated destination\n")
        self.assertEqual((restored / "hooks.json").read_text(), "generated destination\n")
        self.assertEqual((restored / "skills/SKILL.md").read_text(), "generated destination\n")
        for excluded in ("auth.json", "plugins", ".codex-home-prepare.lock"):
            self.assertFalse((restored / excluded).exists(), excluded)
        self.assertEqual(stat.S_IMODE(restored.stat().st_mode), 0o700)
        for path in restored.rglob("*"):
            if path.is_file() and path.relative_to(restored).parts[0] not in {"config.toml", "hooks.json", "skills"}:
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, path)

        state = sqlite3.connect(restored / "state_5.sqlite")
        self.addCleanup(state.close)
        paths = [row[0] for row in state.execute(
            "SELECT rollout_path FROM threads UNION ALL SELECT rollout_path FROM rollout_migration_skipped_rollouts")]
        self.assertEqual(len(paths), 2)
        self.assertTrue(all(path.startswith(str(restored)) and str(self.home) not in path for path in paths))
        history = sqlite3.connect(restored / "thread_history_1.sqlite")
        self.addCleanup(history.close)
        self.assertEqual(history.execute("SELECT value FROM history WHERE id = 'thread-1'").fetchone(), ("durable",))
        for name in ("logs_2.sqlite", "goals_1.sqlite", "memories_1.sqlite", "queue_1.sqlite"):
            self.assertEqual(sqlite3.connect(restored / name).execute("SELECT value FROM retained").fetchone(), (name,))

    def test_unknown_top_level_entry_is_gc_hold_not_a_usable_snapshot(self):
        (self.home / "future-native-artifact.bin").write_bytes(b"unknown must stay at source")
        self.snapshot(expected=2)
        self.assertFalse(self.catalog.exists())
        self.assertFalse(self.receipt.exists())
        self.assertTrue((self.home / "future-native-artifact.bin").is_file())

    # Observed in long-lived canonical homes; every import refused on them.
    RUNTIME_ARTIFACTS = {
        "app-server-control/control.sock": "", "app-server-daemon/daemon.lock": "",
        "process_manager/chat_processes.json": "[]",
        "ambient-suggestions/0a1b/suggestions.json": "{}", "vendor_imports/skills-curated-cache.json": "{}",
        "computer-use/config.json": "{}", "sqlite/codex-dev.db": "app inbox",
        ".codex-global-state.json": "{}", ".codex-global-state.json.bak": "{}",
        "sqlite/codex-dev.db-wal": "", ".cmux-title-sync/abc.lock": "",
        ".glider/last-agents-sync": "1", ".DS_Store": "finder", "rules/default.rules": 'prefix_rule(pattern=["rm"])',
        ".AGENTS.prev.r2mxib": "previous generated", ".AGENTS.md.U7Wm9l": "", ".config.toml.34SyjN": "",
        ".sol.config.toml.Ab12Cd": "", ".hooks.json.NfwGQI": "{}", ".plugin-computer-use.kBXRO6/manifest.json": "{}",
    }

    def test_runtime_state_authority_and_interrupted_writes_are_excluded_not_copied(self):
        for relative, content in self.RUNTIME_ARTIFACTS.items():
            (self.home / relative).parent.mkdir(parents=True, exist_ok=True)
            (self.home / relative).write_text(content)
        (self.home / "tui-luna-reserve").mkdir()
        source_before = digest_tree(self.home)
        receipt = json.loads(self.snapshot().stdout)
        self.assertEqual(digest_tree(self.home), source_before)
        restored = self.base / "restored-home"
        self.run_history("restore", "--catalog", str(self.catalog), "--snapshot", receipt["snapshot_id"],
                         "--destination", str(restored), "--receipt", str(self.base / "restore-receipt.json"))
        tops = {relative.split("/")[0] for relative in self.RUNTIME_ARTIFACTS} | {"tui-luna-reserve"}
        self.assertEqual({path.name for path in restored.iterdir()} & tops, set())
        self.assertTrue((restored / "sessions/2026/10/10" / ("rollout-2026-10-10T01-02-03-" + NATIVE_THREAD_ID + ".jsonl")).is_file())

    def test_external_session_import_ledger_is_kept_with_history(self):
        ledger = '{"records":[{"imported_thread_id":"' + NATIVE_THREAD_ID + '"}]}\n'
        (self.home / "external_agent_session_imports.json").write_text(ledger)
        receipt = json.loads(self.snapshot().stdout)
        restored = self.base / "restored-home"
        self.run_history("restore", "--catalog", str(self.catalog), "--snapshot", receipt["snapshot_id"],
                         "--destination", str(restored), "--receipt", str(self.base / "restore-receipt.json"))
        self.assertEqual((restored / "external_agent_session_imports.json").read_text(), ledger)

    def test_changing_runtime_state_keeps_the_source_inventory_stable(self):
        state = self.home / ".codex-global-state.json"
        state.write_text("{}")
        before = history_module.source_inventory(self.home, history_module.source_children(self.home))
        state.write_text('{"window":"moved"}')
        os.utime(state, ns=(1, 1))
        after = history_module.source_inventory(self.home, history_module.source_children(self.home))
        self.assertEqual(before, after)
        (self.home / "config.toml").write_text('model = "changed"\n')
        self.assertNotEqual(after, history_module.source_inventory(self.home, history_module.source_children(self.home)))

    def test_generic_runtime_directory_shape_decides_exclusion(self):
        sqlite_dir = self.home / "sqlite"
        sqlite_dir.mkdir()
        self.assertTrue(history_module.excluded(sqlite_dir))
        (sqlite_dir / "nested").mkdir()
        self.assertFalse(history_module.excluded(sqlite_dir))
        shutil.rmtree(sqlite_dir)
        target = self.base / "outside.db"
        target.write_text("outside")
        sqlite_dir.mkdir()
        (sqlite_dir / "codex-dev.db").symlink_to(target)
        self.assertFalse(history_module.excluded(sqlite_dir))
        shutil.rmtree(sqlite_dir)
        (self.base / "outside-dir").mkdir()
        sqlite_dir.symlink_to(self.base / "outside-dir")
        self.assertFalse(history_module.excluded(sqlite_dir))
        self.snapshot(expected=2)

    def test_generic_runtime_directories_are_excluded_only_with_known_content(self):
        for relative in ("sqlite/history_9.sqlite", "tui-luna-reserve/native-data"):
            with self.subTest(relative=relative):
                path = self.home / relative
                path.parent.mkdir(exist_ok=True)
                path.write_text("may be data\n")
                self.snapshot(expected=2)
                self.assertFalse(self.catalog.exists())
                shutil.rmtree(path.parent)

    def test_restore_destination_may_hold_runtime_state_but_not_unknown_data(self):
        receipt = json.loads(self.snapshot().stdout)
        destination = self.base / "fresh-home"
        (destination / "app-server-daemon").mkdir(parents=True)
        (destination / "rules").mkdir()
        self.run_history("restore", "--catalog", str(self.catalog), "--snapshot", receipt["snapshot_id"],
                         "--destination", str(destination), "--receipt", str(self.base / "restore-receipt.json"))
        unknown = self.base / "unknown-home"
        (unknown / "future-native-artifact").mkdir(parents=True)
        self.run_history("restore", "--catalog", str(self.catalog), "--snapshot", receipt["snapshot_id"],
                         "--destination", str(unknown), "--receipt", str(self.base / "unknown-receipt.json"), expected=2)

    def test_names_resembling_excluded_state_still_refuse(self):
        # A six-letter word suffix (".AGENTS.md.backup") is indistinguishable from mktemp by design.
        for name in (".config.toml.bak", ".AGENTS.md.bak", "rules-extra", ".hooks.json.ABCDEFG",
                     "sqlite-export", ".glider.old", ".plugin-x.ab"):
            with self.subTest(name=name):
                path = self.home / name
                path.write_text("may be data\n")
                self.snapshot(expected=2)
                self.assertFalse(self.catalog.exists())
                path.unlink()

    def test_missing_provenance_malformed_and_symlink_refuse_without_outputs(self):
        self.run_history("snapshot", "--source", str(self.home), "--catalog", str(self.catalog), "--receipt", str(self.receipt),
                         "--source-root", str(self.source_root), "--isolation-id", ISOLATION_ID, expected=2)
        self.assertFalse(self.catalog.exists())
        self.assertFalse(self.receipt.exists())
        (self.home / "session_index.jsonl").write_text("{not-json}\n")
        self.snapshot(expected=2)
        self.assertFalse(self.catalog.exists())
        self._make_home()
        target = self.base / "outside"
        target.write_text("never follow")
        (self.home / "sessions/link.jsonl").symlink_to(target)
        self.snapshot(expected=2)
        self.assertFalse(self.catalog.exists())
        self.assertFalse(self.receipt.exists())

    def test_actual_active_wal_writer_refuses_without_partial_outputs(self):
        writer = sqlite3.connect(self.home / "thread_history_1.sqlite", timeout=0)
        self.addCleanup(writer.close)
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO history VALUES ('active', 'must-not-copy')")
        self.snapshot(expected=3)
        self.assertEqual(writer.execute("SELECT value FROM history WHERE id = 'active'").fetchone(), ("must-not-copy",))
        self.assertFalse(self.catalog.exists())
        self.assertFalse(self.receipt.exists())

    def test_read_only_source_holds_without_writing_or_archiving(self):
        database = self.home / "thread_history_1.sqlite"
        sidecars = [database, Path(str(database) + "-wal"), Path(str(database) + "-shm")]
        for path in sidecars:
            if path.exists():
                path.chmod(0o400)
        try:
            self.snapshot(expected=3)
            self.assertFalse(self.catalog.exists())
            self.assertFalse(self.receipt.exists())
        finally:
            for path in sidecars:
                if path.exists():
                    path.chmod(0o600)

    def test_catalog_reports_metadata_only_for_the_selected_source(self):
        state = self.base / "state"
        proc = self.run_history("catalog", "--source-root", str(self.source_root), "--state-home", str(state))
        result = json.loads(proc.stdout)
        entry = next(item for item in result["entries"] if item["native_id"] == NATIVE_THREAD_ID)
        self.assertEqual(set(entry), {"native_id", "home", "source_root", "isolation_id", "origin_runtime", "snapshot_id", "updated_at", "status", "reason", "source_local", "origin_state_home"})
        self.assertEqual(entry["source_root"], str(self.source_root))
        self.assertEqual(entry["isolation_id"], "canonical")
        self.assertEqual(entry["origin_runtime"], "legacy-unknown")
        self.assertTrue(entry["source_local"])
        self.assertNotIn("fixture-only-secret", proc.stdout)
        self.assertNotIn("fixture-authority", proc.stdout)

    def test_non_native_legacy_root_without_inode_does_not_hide_native_catalog(self):
        state=self.base/'old-state';identifier=ISOLATION_ID
        root=state/'worktrees'/identifier;root.mkdir(parents=True)
        record=state/'sessions'/identifier;record.mkdir(parents=True)
        (record/'source-root').write_text(str(self.source_root))
        (record/'session-root').write_text(str(root))
        result=json.loads(self.run_history('catalog','--source-root',str(self.source_root),'--state-home',str(state)).stdout)
        self.assertIn(NATIVE_THREAD_ID,[row['native_id'] for row in result['entries']])

    def test_pool_without_native_needs_no_binary_but_native_original_is_held(self):
        state=self.base/'no-native-binary-state'
        result=json.loads(self.run_history('protect-pool','--source-root',str(self.source_root),'--state-home',str(state)).stdout)
        self.assertTrue(result['safe_to_gc'])
        root=state/'worktrees'/ISOLATION_ID;record=state/'sessions'/ISOLATION_ID
        home=root/'.harness/codex';shutil.copytree(self.home,home,symlinks=True);record.mkdir(parents=True)
        for name,value in (('source-root',str(self.source_root)),('session-root',str(root)),
                           ('root-inode',history_module.root_inode(root)),('runtime.lock',''),
                           ('lease-v1','1\n'),('journal','state=CLOSED\nidentity=\nheartbeat=2026-10-10T00:00:00Z\n')):
            (record/name).write_text(value)
        before=digest_tree(home)
        self.run_history('protect-pool','--source-root',str(self.source_root),'--state-home',str(state),expected=2)
        self.assertEqual(digest_tree(home),before)
        self.assertFalse((state/'native-history/snapshots').exists())

    def test_retained_unpublished_staging_does_not_hide_complete_archives(self):
        state=self.base/'staging-state'
        first=json.loads(self.run_history('prepare','--source-root',str(self.source_root),
                        '--state-home',str(state),'--native-id',NATIVE_THREAD_ID).stdout)
        directory=state/'native-history/snapshots'
        shutil.copytree(directory/first['snapshot_id'],directory/'.snapshot-crash-owned')
        result=json.loads(self.run_history('catalog','--source-root',str(self.source_root),'--state-home',str(state)).stdout)
        row=next(row for row in result['entries'] if row['native_id']==NATIVE_THREAD_ID)
        self.assertEqual(row['snapshot_id'],first['snapshot_id'])
        self.assertTrue((directory/'.snapshot-crash-owned/manifest.json').is_file())

    def test_importing_older_archive_does_not_supersede_newer_native_history(self):
        current=self.base/'import-current';legacy=self.base/'import-legacy';store=legacy/'native-history'
        rollout=next(path for path in (self.home/'sessions').rglob('*.jsonl') if 'session_meta' in path.read_text())
        def capture(label):
            return json.loads(self.run_history('snapshot','--source',str(self.home),'--catalog',str(store),
                    '--receipt',str(self.base/(label+'.json')),'--source-root',str(self.source_root),
                    '--isolation-id','canonical','--runtime-revision','legacy-unknown').stdout)['snapshot_id']
        os.utime(rollout,ns=(1000000000,1000000000));older=capture('older')
        rollout.write_text(rollout.read_text()+'{"type":"response_item","id":"newer-history"}\n')
        os.utime(rollout,ns=(2000000000,2000000000));newer=capture('newer')
        shutil.move(self.home,self.base/'retained-original')
        history_module.import_snapshot(store,current/'native-history',older,self.source_root)
        imported=current/'native-history/snapshots'/older
        original=store/'snapshots'/older
        self.assertEqual(digest_tree(imported),digest_tree(original))
        for path in original.rglob('*'):
            if path.is_file():
                self.assertEqual((imported/path.relative_to(original)).stat().st_mtime_ns,path.stat().st_mtime_ns)
        listed=json.loads(self.run_history('catalog','--source-root',str(self.source_root),'--state-home',str(current),
                         '--legacy-state-home',str(legacy)).stdout)
        selected=next(row for row in listed['entries'] if row['native_id']==NATIVE_THREAD_ID)
        self.assertEqual(selected['snapshot_id'],newer,'copy time must not promote older original data')

    def test_snapshot_capture_time_selects_newer_database_with_same_raw_timestamps(self):
        state=self.base/'database-newer';store=state/'native-history'
        def capture(label):
            return json.loads(self.run_history('snapshot','--source',str(self.home),'--catalog',str(store),
                    '--receipt',str(self.base/(label+'.json')),'--source-root',str(self.source_root),
                    '--isolation-id','canonical','--runtime-revision','legacy-unknown').stdout)['snapshot_id']
        older=capture('db-older')
        db=sqlite3.connect(self.home/'thread_history_1.sqlite')
        db.execute("INSERT INTO history VALUES ('additional','newer original database')");db.commit();db.close()
        newer=capture('db-newer');self.assertNotEqual(older,newer)
        shutil.move(self.home,self.base/'retained-db-original')
        listed=json.loads(self.run_history('catalog','--source-root',str(self.source_root),'--state-home',str(state)).stdout)
        selected=next(row for row in listed['entries'] if row['native_id']==NATIVE_THREAD_ID)
        self.assertEqual(selected['snapshot_id'],newer)

    def test_catalog_updated_time_includes_headerless_native_segments(self):
        paths=list((self.home/'sessions').rglob('*.jsonl'))
        for path in paths:os.utime(path,ns=(1000000000,1000000000))
        segment=next(path for path in paths if '_0001' in path.name)
        os.utime(segment,ns=(3000000000,3000000000))
        listed=json.loads(self.run_history('catalog','--source-root',str(self.source_root),'--state-home',str(self.base/'segments-time-state')).stdout)
        selected=next(row for row in listed['entries'] if row['native_id']==NATIVE_THREAD_ID)
        self.assertEqual(int(selected['updated_at']),3000000000)

    def test_prepare_canonical_selected_history_creates_a_snapshot(self):
        state = self.base / "prepare-state"
        proc = self.run_history("prepare", "--source-root", str(self.source_root), "--state-home", str(state), "--native-id", NATIVE_THREAD_ID)
        row = json.loads(proc.stdout)
        self.assertEqual(row["native_id"], NATIVE_THREAD_ID)
        self.assertEqual(row["isolation_id"], "canonical")
        self.assertRegex(row["snapshot_id"], r"^[0-9a-f]{64}$")

    def test_catalog_retains_canonical_prepare_snapshot_and_rejects_stale_receipt(self):
        state = self.base / "prepare-catalog-state"
        first = json.loads(self.run_history(
            "prepare", "--source-root", str(self.source_root), "--state-home", str(state), "--native-id", NATIVE_THREAD_ID
        ).stdout)
        listed = json.loads(self.run_history(
            "catalog", "--source-root", str(self.source_root), "--state-home", str(state)
        ).stdout)
        selected = next(row for row in listed["entries"] if row["native_id"] == NATIVE_THREAD_ID)
        self.assertEqual(selected["snapshot_id"], first["snapshot_id"])
    def test_changed_canonical_source_does_not_reuse_prepare_receipt(self):
        state = self.base / "prepare-change-state"
        first = json.loads(self.run_history(
            "prepare", "--source-root", str(self.source_root), "--state-home", str(state), "--native-id", NATIVE_THREAD_ID
        ).stdout)
        rollout = self.home / "sessions/2026/10/10" / ("rollout-2026-10-10T01-02-03-" + NATIVE_THREAD_ID + ".jsonl")
        rollout.write_text(rollout.read_text() + '{"type":"response_item","id":"new"}\n')
        second = json.loads(self.run_history(
            "prepare", "--source-root", str(self.source_root), "--state-home", str(state), "--native-id", NATIVE_THREAD_ID
        ).stdout)
        self.assertNotEqual(second["snapshot_id"], first["snapshot_id"], "changed source must not reuse an old prepare receipt")

    def test_tampered_snapshot_manifest_is_rejected_before_restore(self):
        snapshot = json.loads(self.snapshot().stdout)["snapshot_id"]
        manifest = self.catalog / "snapshots" / snapshot / "manifest.json"
        value = json.loads(manifest.read_text())
        value["source_root"] = "/tampered"
        manifest.write_text(json.dumps(value))
        self.run_history("restore", "--catalog", str(self.catalog), "--snapshot", snapshot,
                         "--destination", str(self.base / "tampered-restore"), "--receipt", str(self.base / "tampered-receipt"), expected=2)

    def test_catalog_and_prepare_accept_native_uuid_v7(self):
        native_v7 = "01a12390-1234-7311-8123-123456789abc"
        rollout = self.home / "sessions/2026/10/10" / ("rollout-2026-10-10T01-02-03-" + NATIVE_THREAD_ID + ".jsonl")
        row = json.loads(rollout.read_text().splitlines()[0]); row["payload"]["id"] = native_v7
        rollout.write_text(json.dumps(row) + "\n")
        listed = json.loads(self.run_history("catalog", "--source-root", str(self.source_root), "--state-home", str(self.base / "v7-state")).stdout)
        self.assertIn(native_v7, [entry["native_id"] for entry in listed["entries"]])
        prepared = json.loads(self.run_history("prepare", "--source-root", str(self.source_root), "--state-home", str(self.base / "v7-state"), "--native-id", native_v7).stdout)
        self.assertEqual(prepared["native_id"], native_v7)

    def test_nested_native_directory_symlink_refuses_snapshot(self):
        outside = self.base / "outside-native"; outside.mkdir()
        (self.home / "sessions/2026/10/10/linked-directory").symlink_to(outside, target_is_directory=True)
        self.snapshot(expected=2)
        self.assertFalse(self.catalog.exists())
        self.assertFalse(self.receipt.exists())

    def test_orphan_sqlite_wal_is_not_silently_excluded_from_archive(self):
        (self.home/'state_99.sqlite-wal').write_bytes(b'owned orphan native sentinel')
        self.snapshot(expected=2)
        self.assertFalse(self.receipt.exists())

    def test_foreign_harness_ancestor_symlink_refuses_snapshot_and_catalog(self):
        foreign = self.base / "foreign-source"; foreign.mkdir()
        foreign_harness = foreign / ".harness"; shutil.copytree(self.source_root / ".harness", foreign_harness, symlinks=True)
        shutil.rmtree(self.source_root / ".harness")
        (self.source_root / ".harness").symlink_to(foreign_harness, target_is_directory=True)
        self.snapshot(expected=2)
        self.assertFalse(self.catalog.exists())
        self.assertFalse(self.receipt.exists())
        self.run_history("catalog", "--source-root", str(self.source_root), "--state-home", str(self.base / "symlink-state"), expected=2)

    def test_bound_isolated_foreign_harness_ancestor_symlink_refuses_snapshot(self):
        state = self.base / "isolated-state"; identifier = "33333333-2222-4333-8444-555555555556"
        root = state / "worktrees" / identifier; record = state / "sessions" / identifier
        foreign = self.base / "isolated-foreign"; shutil.copytree(self.source_root / ".harness", foreign / ".harness", symlinks=True)
        root.mkdir(parents=True); (root / ".harness").symlink_to(foreign / ".harness", target_is_directory=True)
        record.mkdir(parents=True)
        for name, value in (("source-root", str(self.source_root)), ("session-root", str(root)),
                            ("root-inode", history_module.root_inode(root)),
                            ("journal", "state=CLOSED\nidentity=\nheartbeat=2026-10-10T00:00:00Z\n"),
                            ("lease-v1", "1\n"), ("runtime.lock", "")):
            (record / name).write_text(value)
        self.run_history("snapshot", "--source", str(root / ".harness/codex"), "--catalog", str(state / "native-history"),
                         "--receipt", str(record / "receipt.json"), "--source-root", str(self.source_root),
                         "--isolation-id", identifier, "--runtime-revision", RUNTIME_REVISION, expected=2)

    def test_profile_catalog_finds_shared_old_owner_with_original_state_provenance(self):
        shared=self.base.resolve()/'session-state';current=shared/'gamma';uid=ISOLATION_ID
        root=shared/'worktrees'/uid;native=root/'.harness/codex'
        shutil.copytree(self.home,native,symlinks=True);shutil.rmtree(self.home)
        self._pool_record(shared,uid,self.source_root.resolve())
        registry=self.base/'.config/harness-launcher/profiles';registry.mkdir(parents=True)
        (self.source_root/'config').mkdir();(self.source_root/'config/launcher.env').write_text('fixture')
        (registry/'gamma').write_text(str(self.source_root)+'\n')
        with mock.patch.dict(os.environ,HOME=str(self.base.resolve())):
            value=json.loads(self.run_history('catalog','--source-root',str(self.source_root.resolve()),
                '--state-home',str(current),'--pool-base',str(shared)).stdout)
        row=next(r for r in value['entries'] if r['native_id']==NATIVE_THREAD_ID)
        self.assertEqual(row['home'],str(native))
        self.assertEqual(row['origin_state_home'],str(shared))
        self.assertEqual(row['isolation_id'],uid)
        with mock.patch.dict(os.environ,HOME=str(self.base.resolve())):
            self.run_history('catalog','--source-root',str(self.source_root.resolve()),
                '--state-home',str(self.base.resolve()/'foreign-current'),
                '--pool-base',str(shared),expected=2)

    def test_legacy_state_catalog_and_prepare_preserve_selected_archive(self):
        legacy = self.base / "legacy-state"; identifier = "11111111-2222-4333-8444-555555555556"
        legacy_native = "22222222-2222-4333-8444-555555555556"
        root = legacy / "worktrees" / identifier; record = legacy / "sessions" / identifier
        legacy_home = root / ".harness/codex"; shutil.copytree(self.home, legacy_home, symlinks=True)
        rollout = next(path for path in (legacy_home / "sessions").rglob("*.jsonl") if "session_meta" in path.read_text())
        first, *rest = rollout.read_text().splitlines(); meta = json.loads(first); meta["payload"]["id"] = legacy_native
        rollout.write_text("\n".join([json.dumps(meta), *rest]) + "\n")
        record.mkdir(parents=True)
        for name, value in (("source-root", str(self.source_root)), ("session-root", str(root)),
                            ("root-inode", history_module.root_inode(root)),
                            ("journal", "state=CLOSED\nidentity=\nheartbeat=2026-10-10T00:00:00Z\n"),
                            ("lease-v1", "1\n"), ("runtime.lock", "")):
            (record / name).write_text(value)
        current = self.base / "current-state"
        listed = json.loads(self.run_history("catalog", "--source-root", str(self.source_root), "--state-home", str(current),
                                             "--legacy-state-home", str(legacy)).stdout)
        entry = next(row for row in listed["entries"] if row["native_id"] == legacy_native and row["home"] == str(legacy_home))
        self.assertEqual(entry["isolation_id"], identifier)
        prepared = self.run_history("prepare", "--source-root", str(self.source_root), "--state-home", str(current),
                                    "--legacy-state-home", str(legacy), "--native-id", legacy_native)
        result = json.loads(prepared.stdout)
        self.assertRegex(result["snapshot_id"], r"^[0-9a-f]{64}$")
        self.assertTrue((current / "native-history/snapshots" / result["snapshot_id"] / "data").is_dir())

    def test_protect_pool_masks_then_restores_on_verifier_failure(self):
        state = self.base / "pool-state"; identifier = ISOLATION_ID
        record = state / "sessions" / identifier; root = state / "worktrees" / identifier
        (root / ".harness/codex").mkdir(parents=True); record.mkdir(parents=True)
        for name, value in (("source-root", str(self.source_root)), ("session-root", str(root)),
                            ("root-inode", history_module.root_inode(root)), ("journal", "state=CLOSED\nidentity=\nheartbeat=2026-10-10T00:00:00Z\n"),
                            ("lease-v1", "1\n"), ("runtime.lock", "")):
            (record / name).write_text(value)
        seen = []
        def fake_snapshot(args):
            Path(args.receipt).write_text(json.dumps({"snapshot_id": "a" * 64}))
            print(json.dumps({"snapshot_id": "a" * 64}))
        def fake_verify(args):
            seen.append((Path(args.source).exists(), root.exists()))
            raise history_module.Refusal("fixture verifier failure")
        args = type("Args", (), {"state_home": str(state), "source_root": str(self.source_root), "codex_bin": "/bin/true"})()
        with mock.patch.object(history_module, "snapshot", fake_snapshot), mock.patch.object(history_module, "verify", fake_verify):
            with self.assertRaises(history_module.Refusal):
                history_module.protect_pool(args)
        self.assertEqual(seen, [(True, False)])
        self.assertTrue((root / ".harness/codex").is_dir())

    def test_repeated_restore_keeps_origin_while_execution_runtime_changes(self):
        state=self.base/'repeat-state';store=state/'native-history'
        first=json.loads(self.run_history('prepare','--source-root',str(self.source_root),
                         '--state-home',str(state),'--native-id',NATIVE_THREAD_ID).stdout)
        revision='b'*40;runtime=state.parent/revision;runtime.mkdir()
        (runtime/'native-placeholder').write_text('fixture runtime bytes')
        manifest={'revision':revision,'version':'0.51.0','files':{'native-placeholder':{
                  'sha256':hashlib.sha256((runtime/'native-placeholder').read_bytes()).hexdigest()}}}
        (runtime/'manifest.json').write_text(json.dumps(manifest))
        bindings=state.parent/'bindings';bindings.mkdir()
        previous=first
        for identifier in ('11111111-2222-4333-8444-555555555557','11111111-2222-4333-8444-555555555558'):
            root=state/'worktrees'/identifier;record=state/'sessions'/identifier
            (root/'.harness/codex').mkdir(parents=True);record.mkdir(parents=True)
            for name,value in (('source-root',str(self.source_root)),('session-root',str(root)),
                               ('root-inode',history_module.root_inode(root)),('runtime.lock',''),
                               ('lease-v1','1\n'),('journal','state=CLOSED\nidentity=\nheartbeat=2026-10-10T00:00:00Z\n')):
                (record/name).write_text(value)
            binding={'source_root':str(self.source_root),'runtime':{'revision':revision,'version':'0.51.0',
                     'path':str(runtime),'manifest_sha256':hashlib.sha256((runtime/'manifest.json').read_bytes()).hexdigest()}}
            file=bindings/(identifier+'.json');file.write_text(json.dumps(binding));file.chmod(0o600)
            self.run_history('restore','--catalog',str(store),'--snapshot',previous['snapshot_id'],
                             '--destination',str(root/'.harness/codex'),'--receipt',str(record/'native-history-restore.json'),
                             '--native-id',NATIVE_THREAD_ID,'--origin-runtime',previous['origin_runtime'])
            prepared=json.loads(self.run_history('prepare','--source-root',str(self.source_root),
                                '--state-home',str(state),'--native-id',NATIVE_THREAD_ID).stdout)
            self.assertEqual(prepared['origin_runtime'],'legacy-unknown')
            saved=json.loads((store/'snapshots'/prepared['snapshot_id']/'manifest.json').read_text())
            self.assertEqual(saved['runtime_revision'],revision)
            self.assertEqual(saved['origin_runtime'],'legacy-unknown')
            listed=json.loads(self.run_history('catalog','--source-root',str(self.source_root),'--state-home',str(state)).stdout)
            row=next(row for row in listed['entries'] if row['native_id']==NATIVE_THREAD_ID)
            self.assertEqual(row['snapshot_id'],prepared['snapshot_id'])
            self.assertEqual(row['origin_runtime'],prepared['origin_runtime'])
            previous=prepared

    def test_protect_pool_calls_verifier_then_restores_terminal_root(self):
        state = self.base / "pool-success"; identifier = ISOLATION_ID
        record = state / "sessions" / identifier; root = state / "worktrees" / identifier
        (root / ".harness/codex").mkdir(parents=True); record.mkdir(parents=True)
        for name, value in (("source-root", str(self.source_root)), ("session-root", str(root)),
                            ("root-inode", history_module.root_inode(root)), ("journal", "state=CLOSED\nidentity=\nheartbeat=2026-10-10T00:00:00Z\n"),
                            ("lease-v1", "1\n"), ("runtime.lock", "")):
            (record / name).write_text(value)
        called = []
        def fake_snapshot(args):
            Path(args.receipt).write_text(json.dumps({"snapshot_id": "b" * 64})); print(json.dumps({"snapshot_id": "b" * 64}))
        def fake_verify(args):
            called.append((Path(args.source).exists(), root.exists()))
        args = type("Args", (), {"state_home": str(state), "source_root": str(self.source_root), "codex_bin": "/bin/true"})()
        with mock.patch.object(history_module, "snapshot", fake_snapshot), mock.patch.object(history_module, "verify", fake_verify):
            history_module.protect_pool(args)
        self.assertEqual(called, [(True, False)])
        self.assertTrue((root / ".harness/codex").is_dir())
        self.assertEqual((record / "journal").read_text(), "state=CLOSED\nidentity=\nheartbeat=2026-10-10T00:00:00Z\n")

    def _pool_record(self, state, identifier, owner, root=None):
        record = state / "sessions" / identifier; expected = state / "worktrees" / identifier
        record.mkdir(parents=True)
        for name, value in (("source-root", str(owner)), ("session-root", str(expected)),
                            ("root-inode", history_module.root_inode(root or expected)),
                            ("journal", "state=CLOSED\nidentity=\nheartbeat=2026-10-10T00:00:00Z\n"),
                            ("lease-v1", "1\n"), ("runtime.lock", "")):
            (record / name).write_text(value)
        return record

    def test_protect_pool_protects_other_profile_root_under_its_own_source(self):
        # Every profile shares one legacy pool and the old GC is global.
        state = self.base / "shared-pool"; other = self.base / "other-profile"; other.mkdir()
        root = state / "worktrees" / ISOLATION_ID
        shutil.copytree(self.home, root / ".harness/codex", symlinks=True)
        self._pool_record(state, ISOLATION_ID, other)
        verified = []
        def fake_verify(args):
            verified.append((args.snapshot, Path(args.source).exists(), root.exists()))
        args = type("Args", (), {"state_home": str(state), "source_root": str(self.source_root), "codex_bin": "/bin/true"})()
        with mock.patch.object(history_module, "verify", fake_verify), contextlib.redirect_stdout(io.StringIO()):
            history_module.protect_pool(args)
        manifests = [json.loads(path.read_text()) for path in (state / "native-history/snapshots").glob("*/manifest.json")]
        self.assertEqual([(m["source_root"], m["isolation_id"]) for m in manifests], [(str(other), ISOLATION_ID)])
        self.assertEqual(verified, [(manifests[0]["snapshot_id"], True, False)])
        self.assertTrue((root / ".harness/codex").is_dir())

    def test_protect_pool_recovers_other_profile_tomb_before_protecting_it(self):
        state = self.base / "shared-tomb-pool"; other = self.base / "other-profile"; other.mkdir()
        tomb = state / "worktrees" / (".archiving-" + ISOLATION_ID + "-123")
        (tomb / ".harness/codex").mkdir(parents=True)
        self._pool_record(state, ISOLATION_ID, other, root=tomb)
        owners = []
        def fake_snapshot(args):
            owners.append(args.source_root)
            Path(args.receipt).write_text(json.dumps({"snapshot_id": "c" * 64})); print(json.dumps({"snapshot_id": "c" * 64}))
        args = type("Args", (), {"state_home": str(state), "source_root": str(self.source_root), "codex_bin": "/bin/true"})()
        with mock.patch.object(history_module, "snapshot", fake_snapshot), mock.patch.object(history_module, "verify", lambda args: None):
            history_module.protect_pool(args)
        self.assertEqual(owners, [str(other)])
        self.assertFalse(tomb.exists())
        self.assertTrue((state / "worktrees" / ISOLATION_ID / ".harness/codex").is_dir())

    def test_protect_pool_refuses_unnormalized_recorded_source_without_snapshot(self):
        for owner in ("relative/profile", str(self.base / "other") + "/", str(self.base / "x/../other"),
                      "/" + str(self.base / "other"), str(self.base)):
            for tombed in (False, True):
                state = self.base / ("unsafe-owner-" + hashlib.sha256((owner + str(tombed)).encode()).hexdigest()[:8])
                root = state / "worktrees" / ((".archiving-" + ISOLATION_ID + "-123") if tombed else ISOLATION_ID)
                (root / ".harness/codex").mkdir(parents=True)
                self._pool_record(state, ISOLATION_ID, owner, root=root)
                args = type("Args", (), {"state_home": str(state), "source_root": str(self.source_root), "codex_bin": "/bin/true"})()
                with self.subTest(owner=owner, tombed=tombed), \
                        mock.patch.object(history_module, "snapshot", side_effect=AssertionError("snapshot")):
                    with self.assertRaisesRegex(history_module.Refusal, "unsafe recorded source root"):
                        history_module.protect_pool(args)
                    self.assertTrue((root / ".harness/codex").is_dir())

    def test_protect_pool_binds_each_record_and_import_ledger_to_its_own_owner(self):
        state = self.base / "mixed-pool"; other = self.base / "other-profile"; other.mkdir()
        mine, theirs = ISOLATION_ID, "11111111-2222-4333-8444-555555555559"
        for identifier, owner in ((mine, self.source_root), (theirs, other)):
            shutil.copytree(self.home, state / "worktrees" / identifier / ".harness/codex", symlinks=True)
            self._pool_record(state, identifier, owner)
        ledger = state / "sessions" / theirs / "restored-native-sessions"
        def bind_ledger(root):
            ledger.write_text(json.dumps({"schema": 1, "session_id": theirs, "source_root": str(root.resolve()),
                "imports": [{"runtime": "codex", "native_id": PARENT_THREAD_ID, "source_owner": mine}]}))
            ledger.chmod(0o600)
        bind_ledger(other)
        args = type("Args", (), {"state_home": str(state), "source_root": str(self.source_root), "codex_bin": "/bin/true"})()
        with mock.patch.object(history_module, "verify", lambda args: None), contextlib.redirect_stdout(io.StringIO()):
            history_module.protect_pool(args)
        manifests = {m["isolation_id"]: m for m in (json.loads(path.read_text())
                     for path in (state / "native-history/snapshots").glob("*/manifest.json"))}
        self.assertEqual({key: value["source_root"] for key, value in manifests.items()},
                         {mine: str(self.source_root), theirs: str(other)})
        self.assertEqual(manifests[theirs]["native_import_provenance"]["imports"][0]["source_owner"], mine)
        bind_ledger(self.source_root)
        with mock.patch.object(history_module, "verify", lambda args: None), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(history_module.Refusal, "invalid imported Native ledger"):
                history_module.protect_pool(args)

    def test_catalog_warns_for_missing_or_mismatched_legacy_native_inode_without_hiding_canonical(self):
        state = self.base / "legacy-inode-warnings"
        for identifier, inode in (("11111111-2222-4333-8444-555555555556", None),
                                  ("11111111-2222-4333-8444-555555555557", "0 0\n")):
            root = state / "worktrees" / identifier
            record = state / "sessions" / identifier
            shutil.copytree(self.home, root / ".harness/codex", symlinks=True)
            record.mkdir(parents=True)
            (record / "source-root").write_text(str(self.source_root))
            (record / "session-root").write_text(str(root))
            if inode is not None:
                (record / "root-inode").write_text(inode)
        result = json.loads(self.run_history("catalog", "--source-root", str(self.source_root), "--state-home", str(state)).stdout)
        self.assertIn(NATIVE_THREAD_ID, [row["native_id"] for row in result["entries"]])
        self.assertEqual({(warning["reason"], warning["isolation_id"], warning["pool"]) for warning in result["warnings"]}, {
            ("root_inode_missing", "11111111-2222-4333-8444-555555555556", "current"),
            ("root_inode_mismatch", "11111111-2222-4333-8444-555555555557", "current"),
        })

    def test_prepare_refuses_unverified_inode_without_snapshot_or_native_mutation(self):
        state = self.base / "unverified-prepare"
        identifier = "11111111-2222-4333-8444-555555555556"
        native = "22222222-2222-4333-8444-555555555556"
        root, record = state / "worktrees" / identifier, state / "sessions" / identifier
        shutil.copytree(self.home, root / ".harness/codex", symlinks=True)
        rollout = next(path for path in (root / ".harness/codex/sessions").rglob("*.jsonl") if "session_meta" in path.read_text())
        value = json.loads(rollout.read_text().splitlines()[0]); value["payload"]["id"] = native
        rollout.write_text(json.dumps(value) + "\n")
        record.mkdir(parents=True)
        (record / "source-root").write_text(str(self.source_root))
        (record / "session-root").write_text(str(root))
        before = digest_tree(root / ".harness/codex")
        self.run_history("prepare", "--source-root", str(self.source_root), "--state-home", str(state), "--native-id", native, expected=2)
        self.assertEqual(digest_tree(root / ".harness/codex"), before)
        self.assertFalse((state / "native-history/snapshots").exists())

    def test_filename_collision_marks_native_uuid_ambiguous_and_prepare_holds(self):
        state = self.base / "filename-collision"
        identifier = "11111111-2222-4333-8444-555555555556"
        root, record = state / "worktrees" / identifier, state / "sessions" / identifier
        shutil.copytree(self.home, root / ".harness/codex", symlinks=True)
        record.mkdir(parents=True)
        (record / "source-root").write_text(str(self.source_root))
        (record / "session-root").write_text(str(root))
        listed = json.loads(self.run_history("catalog", "--source-root", str(self.source_root), "--state-home", str(state)).stdout)
        row = next(item for item in listed["entries"] if item["native_id"] == NATIVE_THREAD_ID)
        self.assertEqual((row["status"], row["reason"]), ("ambiguous", "unverified_native_owner"))
        self.run_history("prepare", "--source-root", str(self.source_root), "--state-home", str(state), "--native-id", NATIVE_THREAD_ID, expected=2)
        self.assertFalse((state / "native-history/snapshots").exists())

    def test_quarantined_rollout_links_fail_catalog_and_prepare_without_body_read(self):
        for kind in ("symlink", "hardlink"):
            state = self.base / ("quarantined-" + kind)
            identifier = "11111111-2222-4333-8444-555555555556"
            root, record = state / "worktrees" / identifier, state / "sessions" / identifier
            home = root / ".harness/codex"
            shutil.copytree(self.home, home, symlinks=True)
            record.mkdir(parents=True)
            (record / "source-root").write_text(str(self.source_root))
            (record / "session-root").write_text(str(root))
            outside = self.base / ("quarantined-" + kind + ".jsonl")
            outside.write_text('{"fixture-only-secret":"must not be read"}\n')
            target = home / "sessions/2026/10/10/quarantined.jsonl"
            if kind == "symlink":
                target.symlink_to(outside)
            else:
                os.link(outside, target)
            proc = self.run_history("catalog", "--source-root", str(self.source_root), "--state-home", str(state), expected=2)
            self.assertNotIn("fixture-only-secret", proc.stdout + proc.stderr)
            self.run_history("prepare", "--source-root", str(self.source_root), "--state-home", str(state), "--native-id", NATIVE_THREAD_ID, expected=2)

    def test_quarantined_malformed_body_is_not_read_and_keeps_canonical_stable(self):
        state = self.base / "quarantined-body"
        identifier = "11111111-2222-4333-8444-555555555556"
        quarantined = "22222222-2222-4333-8444-555555555556"
        root, record = state / "worktrees" / identifier, state / "sessions" / identifier
        home = root / ".harness/codex"
        (home / "sessions/2026/10/10").mkdir(parents=True)
        body = home / "sessions/2026/10/10" / ("rollout-quarantined-" + quarantined + ".jsonl")
        body.write_text("{ malformed native body\n")
        record.mkdir(parents=True)
        (record / "source-root").write_text(str(self.source_root))
        (record / "session-root").write_text(str(root))
        original_open = Path.open
        def guarded_open(path, *args, **kwargs):
            if path == body:
                raise AssertionError("catalog read quarantined native body")
            return original_open(path, *args, **kwargs)
        args = type("Args", (), {"source_root": str(self.source_root), "state_home": str(state), "legacy_state_home": None})()
        with mock.patch.object(Path, "open", guarded_open):
            listed = history_module.catalog_value(args)
        row = next(item for item in listed["entries"] if item["native_id"] == NATIVE_THREAD_ID)
        self.assertEqual((row["status"], row["reason"]), ("canonical", None))
        self.assertIn("root_inode_missing", [warning["reason"] for warning in listed["warnings"]])

    def test_inode_change_after_catalog_makes_prepare_hold(self):
        state = self.base / "inode-changed-after-catalog"
        identifier = "11111111-2222-4333-8444-555555555556"
        native = "22222222-2222-4333-8444-555555555556"
        root, record = state / "worktrees" / identifier, state / "sessions" / identifier
        shutil.copytree(self.home, root / ".harness/codex", symlinks=True)
        rollout = next(path for path in (root / ".harness/codex/sessions").rglob("*.jsonl") if "session_meta" in path.read_text())
        value = json.loads(rollout.read_text().splitlines()[0]); value["payload"]["id"] = native
        rollout.write_text(json.dumps(value) + "\n")
        record.mkdir(parents=True)
        for name, value in (("source-root", str(self.source_root)), ("session-root", str(root)),
                            ("root-inode", history_module.root_inode(root)), ("runtime.lock", ""),
                            ("lease-v1", "1\n"), ("journal", "state=CLOSED\nidentity=\nheartbeat=2026-10-10T00:00:00Z\n")):
            (record / name).write_text(value)
        self.run_history("catalog", "--source-root", str(self.source_root), "--state-home", str(state))
        (record / "root-inode").write_text("0 0\n")
        self.run_history("prepare", "--source-root", str(self.source_root), "--state-home", str(state), "--native-id", native, expected=2)
        self.assertFalse((state / "native-history/snapshots").exists())

    def test_protect_pool_holds_unreadable_native_root_and_tomb_without_mutation(self):
        state = self.base / "unreadable-protect-pool"
        identifier = "11111111-2222-4333-8444-555555555556"
        root, record = state / "worktrees" / identifier, state / "sessions" / identifier
        home = root / ".harness/codex"
        (home / "sessions").mkdir(parents=True)
        (home / "sessions/native.jsonl").write_text("native sentinel\n")
        record.mkdir(parents=True)
        for name, value in (("source-root", str(self.source_root)), ("session-root", str(root)),
                            ("root-inode", history_module.root_inode(root)), ("runtime.lock", ""),
                            ("lease-v1", "1\n"), ("journal", "state=CLOSED\nidentity=\nheartbeat=2026-10-10T00:00:00Z\n")):
            (record / name).write_text(value)
        before = digest_tree(home)
        root_inode_before = history_module.root_inode(root)
        denied = root / ".harness"
        denied.chmod(0)
        try:
            self.run_history("protect-pool", "--source-root", str(self.source_root), "--state-home", str(state), "--codex-bin", "/bin/true", expected=2)
        finally:
            denied.chmod(0o700)
        self.assertEqual(digest_tree(home), before)
        self.assertTrue(root.exists())
        self.assertEqual(history_module.root_inode(root), root_inode_before)
        self.assertFalse((state / "native-history/snapshots").exists())

        tomb_state = self.base / "unreadable-unrecorded-native-tomb"
        (tomb_state / "sessions").mkdir(parents=True)
        tomb = tomb_state / "worktrees/.retired-33333333-3333-4333-8333-333333333333-77"
        (tomb / ".harness/codex/sessions").mkdir(parents=True)
        sentinel = tomb / ".harness/codex/sessions/native.jsonl"
        sentinel.write_text("unrecorded native tomb sentinel\n")
        tomb_digest = digest_tree(tomb)
        (tomb / ".harness").chmod(0)
        try:
            self.run_history("protect-pool", "--source-root", str(self.source_root), "--state-home", str(tomb_state), "--codex-bin", "/bin/true", expected=2)
        finally:
            (tomb / ".harness").chmod(0o700)
        self.assertEqual(digest_tree(tomb), tomb_digest)


    def test_protect_pool_holds_symlinked_native_tomb_in_an_independent_pool(self):
        state = self.base / "symlinked-native-tomb"
        linked = state / "worktrees/.retired-44444444-4444-4444-8444-444444444444-78"
        linked.mkdir(parents=True)
        (state / "sessions").mkdir()
        foreign = self.base / "foreign-harness"; (foreign / "codex").mkdir(parents=True)
        (linked / ".harness").symlink_to(foreign, target_is_directory=True)
        self.run_history("protect-pool", "--source-root", str(self.source_root), "--state-home", str(state), "--codex-bin", "/bin/true", expected=2)

    def test_protect_pool_allows_non_native_pool_without_codex_binary(self):
        state = self.base / "non-native-pool"
        (state / "worktrees").mkdir(parents=True)
        (state / "sessions").mkdir()
        (state / "worktrees/.DS_Store").write_text("non-native fixture")
        (state / "worktrees/.retired-11111111-2222-4333-8444-555555555555-42").write_text("regular non-native tomb lookalike")
        identifier = "11111111-2222-4333-8444-555555555556"
        root, record = state / "worktrees" / identifier, state / "sessions" / identifier
        root.mkdir(); record.mkdir()
        (record / "source-root").write_text(str(self.source_root))
        (record / "session-root").write_text(str(root))
        result = json.loads(self.run_history("protect-pool", "--source-root", str(self.source_root), "--state-home", str(state), "--codex-bin", "/no/codex/binary").stdout)
        self.assertTrue(result["safe_to_gc"])

    def test_generic_import_ledger_suppresses_only_parent_native_uuid_from_new_owner(self):
        state = self.base / "generic-import-ledger"
        old_owner = "11111111-2222-4333-8444-555555555556"
        new_owner = "11111111-2222-4333-8444-555555555557"
        child_native = "22222222-2222-4333-8444-555555555556"
        def owner(identifier, native_ids):
            root, record = state / "worktrees" / identifier, state / "sessions" / identifier
            home = root / ".harness/codex"
            shutil.copytree(self.home, home, symlinks=True)
            rollout = next(path for path in (home / "sessions").rglob("*.jsonl") if "session_meta" in path.read_text())
            value = json.loads(rollout.read_text().splitlines()[0]); value["payload"]["id"] = native_ids[0]
            rollout.write_text(json.dumps(value) + "\n")
            for native in native_ids[1:]:
                (rollout.parent / ("rollout-child-" + native + ".jsonl")).write_text(json.dumps({"type":"session_meta","payload":{"id":native}}) + "\n")
            record.mkdir(parents=True)
            for name, value in (("source-root", str(self.source_root)), ("session-root", str(root)),
                                ("root-inode", history_module.root_inode(root)), ("runtime.lock", ""),
                                ("lease-v1", "1\n"), ("journal", "state=CLOSED\nidentity=\nheartbeat=2026-10-10T00:00:00Z\n")):
                (record / name).write_text(value)
            return root, record
        old_root, _ = owner(old_owner, [NATIVE_THREAD_ID])
        new_root, new_record = owner(new_owner, [NATIVE_THREAD_ID, child_native])
        ledger = {"schema": 1, "session_id": new_owner, "source_root": str(self.source_root.resolve()),
                  "imports": [{"runtime": "codex", "native_id": NATIVE_THREAD_ID, "source_owner": old_owner}]}
        ledger_path = new_record / "restored-native-sessions"
        ledger_path.write_text(json.dumps(ledger, sort_keys=True) + "\n")
        ledger_path.chmod(0o600)
        shutil.move(self.home, self.base / "hidden-canonical-native-home")
        listed = json.loads(self.run_history("catalog", "--source-root", str(self.source_root), "--state-home", str(state)).stdout)
        parent = next(row for row in listed["entries"] if row["native_id"] == NATIVE_THREAD_ID)
        self.assertEqual((parent["status"], parent["home"]), ("live", str(old_root / ".harness/codex")))
        child = next(row for row in listed["entries"] if row["native_id"] == child_native)
        self.assertEqual((child["status"], child["home"]), ("live", str(new_root / ".harness/codex")))
        prepared = json.loads(self.run_history("prepare", "--source-root", str(self.source_root), "--state-home", str(state), "--native-id", child_native).stdout)
        manifest = json.loads((state / "native-history/snapshots" / prepared["snapshot_id"] / "manifest.json").read_text())
        self.assertEqual(manifest["native_import_provenance"]["imports"], [{"runtime": "codex", "native_id": NATIVE_THREAD_ID, "source_owner": old_owner}])
        parent_raw = next(path for path in (new_root / ".harness/codex/sessions").rglob("*.jsonl") if NATIVE_THREAD_ID in path.name)
        archived_parent = state / "native-history/snapshots" / prepared["snapshot_id"] / "data" / parent_raw.relative_to(new_root / ".harness/codex")
        self.assertTrue(archived_parent.is_file())
        self.assertEqual(archived_parent.read_bytes(), parent_raw.read_bytes())
        shutil.move(new_root / ".harness/codex", self.base / "masked-imported-child-home")
        after_mask = json.loads(self.run_history("catalog", "--source-root", str(self.source_root), "--state-home", str(state)).stdout)
        parent_rows = [row for row in after_mask["entries"] if row["native_id"] == NATIVE_THREAD_ID]
        child_rows = [row for row in after_mask["entries"] if row["native_id"] == child_native]
        self.assertEqual([(row["status"], row["home"]) for row in parent_rows], [("live", str(old_root / ".harness/codex"))])
        self.assertEqual([(row["status"], row["snapshot_id"]) for row in child_rows], [("snapshot", prepared["snapshot_id"])])

    def test_invalid_generic_import_ledger_holds_before_child_native_body_parse(self):
        state = self.base / "invalid-generic-import-ledger"
        identifier = "11111111-2222-4333-8444-555555555556"
        root, record = state / "worktrees" / identifier, state / "sessions" / identifier
        home = root / ".harness/codex"
        (home / "sessions/2026/10/10").mkdir(parents=True)
        body = home / "sessions/2026/10/10" / ("rollout-" + NATIVE_THREAD_ID + ".jsonl")
        body.write_text("{ malformed child body\n")
        record.mkdir(parents=True)
        for name, value in (("source-root", str(self.source_root)), ("session-root", str(root)),
                            ("root-inode", history_module.root_inode(root)), ("runtime.lock", ""),
                            ("lease-v1", "1\n"), ("journal", "state=CLOSED\nidentity=\nheartbeat=2026-10-10T00:00:00Z\n")):
            (record / name).write_text(value)
        ledger = record / "restored-native-sessions"
        ledger.write_text(json.dumps({"schema": 1, "session_id": identifier, "source_root": "/wrong-source", "imports": []}) + "\n")
        ledger.chmod(0o600)
        proc = self.run_history("catalog", "--source-root", str(self.source_root), "--state-home", str(state), expected=2)
        self.assertNotIn("malformed child body", proc.stdout + proc.stderr)

    def test_invalid_generic_import_ledgers_hold_before_child_body_parse(self):
        variants = {
            "wrong_source": {"schema": 1, "source_root": "/wrong", "imports": []},
            "same_owner": {"schema": 1, "source_root": str(self.source_root.resolve()), "imports": [{"runtime":"codex", "native_id":NATIVE_THREAD_ID, "source_owner":"11111111-2222-4333-8444-555555555556"}]},
            "unknown_field": {"schema": 1, "source_root": str(self.source_root.resolve()), "imports": [], "unexpected": True},
        }
        for name, value in variants.items():
            with self.subTest(name=name):
                state = self.base / ("strict-import-" + name)
                identifier = "11111111-2222-4333-8444-555555555556"
                root, record = state / "worktrees" / identifier, state / "sessions" / identifier
                home = root / ".harness/codex"
                (home / "sessions/2026/10/10").mkdir(parents=True)
                body = home / "sessions/2026/10/10" / ("rollout-" + NATIVE_THREAD_ID + ".jsonl")
                body.write_text(json.dumps({"type":"session_meta", "payload":{"id":NATIVE_THREAD_ID}}) + "\n")
                record.mkdir(parents=True)
                for field, content in (("source-root", str(self.source_root)), ("session-root", str(root)),
                                       ("root-inode", history_module.root_inode(root)), ("runtime.lock", ""),
                                       ("lease-v1", "1\n"), ("journal", "state=CLOSED\nidentity=\nheartbeat=2026-10-10T00:00:00Z\n")):
                    (record / field).write_text(content)
                value = {**value, "session_id": identifier}
                ledger = record / "restored-native-sessions"
                ledger.write_text(json.dumps(value) + "\n"); ledger.chmod(0o600)
                original_open = Path.open
                def guarded_open(path, *args, **kwargs):
                    if path == body:
                        raise AssertionError("invalid ledger reached child native body")
                    return original_open(path, *args, **kwargs)
                args = type("Args", (), {"source_root": str(self.source_root), "state_home": str(state), "legacy_state_home": None})()
                with mock.patch.object(Path, "open", guarded_open):
                    with self.assertRaisesRegex(history_module.Refusal, "invalid imported Native ledger"):
                        history_module.catalog_value(args)

    def test_isolated_snapshot_requires_exact_original_root_inode_before_native_read(self):
        for label, inode in (("missing", None), ("mismatch", "0 0\n")):
            with self.subTest(label=label):
                state = self.base / ("isolated-snapshot-inode-" + label)
                identifier = "11111111-2222-4333-8444-555555555556"
                root, record = state / "worktrees" / identifier, state / "sessions" / identifier
                home = root / ".harness/codex"
                shutil.copytree(self.home, home, symlinks=True)
                record.mkdir(parents=True)
                (record / "source-root").write_text(str(self.source_root))
                (record / "session-root").write_text(str(root))
                if inode is not None:
                    (record / "root-inode").write_text(inode)
                before = digest_tree(home)
                self.run_history("snapshot", "--source", str(home), "--catalog", str(state / "native-history"),
                                 "--receipt", str(record / "native-history-snapshot.json"), "--source-root", str(self.source_root),
                                 "--isolation-id", identifier, "--runtime-revision", RUNTIME_REVISION, expected=2)
                self.assertEqual(digest_tree(home), before)
                self.assertFalse((state / "native-history/snapshots").exists())
                self.assertFalse((record / "native-history-snapshot.json").exists())

    def test_snapshot_holds_when_valid_import_ledger_changes_during_copy(self):
        state = self.base / "ledger-copy-race"
        identifier, old_owner = "11111111-2222-4333-8444-555555555556", "11111111-2222-4333-8444-555555555557"
        root, record = state / "worktrees" / identifier, state / "sessions" / identifier
        home = root / ".harness/codex"
        shutil.copytree(self.home, home, symlinks=True)
        record.mkdir(parents=True)
        for name, value in (("source-root", str(self.source_root)), ("session-root", str(root)), ("root-inode", history_module.root_inode(root))):
            (record / name).write_text(value)
        ledger = record / "restored-native-sessions"
        ledger.write_text(json.dumps({"schema":1,"session_id":identifier,"source_root":str(self.source_root.resolve()),"imports":[{"runtime":"codex","native_id":NATIVE_THREAD_ID,"source_owner":old_owner}]}) + "\n")
        ledger.chmod(0o600)
        args = type("Args", (), {"source":str(home), "catalog":str(state / "native-history"), "receipt":str(record / "receipt.json"), "source_root":str(self.source_root), "isolation_id":identifier, "runtime_revision":RUNTIME_REVISION})()
        original = history_module.archive_children
        def mutate_after_copy(*values):
            result = original(*values)
            ledger.write_text(ledger.read_text() + "\n")
            return result
        with mock.patch.object(history_module, "archive_children", mutate_after_copy):
            with self.assertRaises(history_module.Refusal):
                history_module.snapshot(args)
        snapshots = state / "native-history/snapshots"
        self.assertFalse(any(path.name[0] != "." for path in snapshots.iterdir()))
        self.assertFalse((record / "receipt.json").exists())

    def test_verify_holds_changed_import_ledger_before_native_proof(self):
        state = self.base / "ledger-verify-race"
        identifier, old_owner = "11111111-2222-4333-8444-555555555556", "11111111-2222-4333-8444-555555555557"
        root, record = state / "worktrees" / identifier, state / "sessions" / identifier
        home = root / ".harness/codex"
        shutil.copytree(self.home, home, symlinks=True)
        record.mkdir(parents=True)
        for name, value in (("source-root", str(self.source_root)), ("session-root", str(root)), ("root-inode", history_module.root_inode(root))):
            (record / name).write_text(value)
        ledger = record / "restored-native-sessions"
        ledger.write_text(json.dumps({"schema":1,"session_id":identifier,"source_root":str(self.source_root.resolve()),"imports":[{"runtime":"codex","native_id":NATIVE_THREAD_ID,"source_owner":old_owner}]}) + "\n")
        ledger.chmod(0o600)
        receipt = record / "receipt.json"
        snap_args = type("Args", (), {"source":str(home), "catalog":str(state / "native-history"), "receipt":str(receipt), "source_root":str(self.source_root), "isolation_id":identifier, "runtime_revision":RUNTIME_REVISION})()
        captured = self.run_history("snapshot", "--source", str(home), "--catalog", str(state / "native-history"), "--receipt", str(receipt), "--source-root", str(self.source_root), "--isolation-id", identifier, "--runtime-revision", RUNTIME_REVISION)
        snapshot = json.loads(captured.stdout)["snapshot_id"]
        ledger.write_text(ledger.read_text() + "\n")
        shutil.move(home, self.base / "masked-ledger-verify-home")
        verify_args = type("Args", (), {"catalog":str(state / "native-history"), "snapshot":snapshot, "source":str(home), "codex_bin":"/bin/true"})()
        called = []
        fake_native = types.SimpleNamespace(NativeVerificationError=RuntimeError,
                                            verify_native=lambda *unused: called.append(True))
        with mock.patch.dict(sys.modules, {"codex_history_native": fake_native}):
            with self.assertRaises(history_module.Refusal):
                history_module.verify(verify_args)
        self.assertEqual(called, [])


    def test_unreadable_quarantined_directory_cannot_hide_a_uuid_collision(self):
        state=self.base/'unreadable-quarantine'
        identifier='11111111-2222-4333-8444-555555555556'
        root=state/'worktrees'/identifier;record=state/'sessions'/identifier
        directory=root/'.harness/codex/sessions';directory.mkdir(parents=True)
        (directory/('rollout-'+NATIVE_THREAD_ID+'.jsonl')).write_text('never parse this body')
        record.mkdir(parents=True)
        (record/'source-root').write_text(str(self.source_root));(record/'session-root').write_text(str(root))
        before={p.name:p.read_bytes() for p in record.iterdir()}
        for denied in (directory,directory.parent,directory.parent.parent,state,root.parent,record,state/'sessions'):
            with self.subTest(denied=str(denied.relative_to(state))):
                denied.chmod(0)
                try:
                    self.run_history('catalog','--source-root',str(self.source_root),'--state-home',str(state),expected=2)
                    self.run_history('prepare','--source-root',str(self.source_root),'--state-home',str(state),'--native-id',NATIVE_THREAD_ID,expected=2)
                finally:denied.chmod(0o700)
        self.assertEqual(before,{p.name:p.read_bytes() for p in record.iterdir()})
        self.assertFalse((state/'native-history/snapshots').exists())

    def test_unreadable_nested_native_data_is_never_omitted_from_snapshot(self):
        denied=self.home/'attachments/private-directory';denied.mkdir()
        (denied/'retained-native-data').write_text('owned native data must remain')
        denied.chmod(0)
        try:self.snapshot(expected=2)
        finally:denied.chmod(0o700)
        self.assertFalse(self.receipt.exists())

    def test_unreadable_canonical_rollout_directory_cannot_hide_a_valid_owner(self):
        denied=self.home/'sessions';denied.chmod(0)
        try:
            self.run_history('catalog','--source-root',str(self.source_root),'--state-home',str(self.base/'unreadable-canonical'),expected=2)
        finally:denied.chmod(0o700)


class RootIdentityTest(unittest.TestCase):
    """Root identity tolerates APFS device renumbering at every codex-history site."""

    setUp = CodexHistoryTest.setUp
    _make_home = CodexHistoryTest._make_home
    _make_auxiliary_db = CodexHistoryTest._make_auxiliary_db
    _make_state_db = CodexHistoryTest._make_state_db
    _make_history_db = CodexHistoryTest._make_history_db

    def fixture(self, name, recorded):
        state = self.base / name
        identifier = ISOLATION_ID
        root, record = state / "worktrees" / identifier, state / "sessions" / identifier
        shutil.copytree(self.home, root / ".harness/codex", symlinks=True)
        record.mkdir(parents=True)
        info = root.stat()
        for key, value in (("source-root", str(self.source_root)), ("session-root", str(root)),
                           ("root-inode", recorded(info)), ("lease-v1", "1\n"), ("runtime.lock", ""),
                           ("journal", "state=CLOSED\nidentity=\nheartbeat=2026-10-10T00:00:00Z\n")):
            (record / key).write_text(value)
        return state, root, record

    RENUMBERED = staticmethod(lambda info: f"{info.st_dev + 1} {info.st_ino}")
    OTHER_INODE = staticmethod(lambda info: f"{info.st_dev + 1} {info.st_ino + 1}")
    SAME = staticmethod(lambda info: f"{info.st_dev} {info.st_ino}")

    def seam_other_device(self, root):
        real = history_module.identity_stat
        def fake(path):
            info = real(path)
            if Path(path) != root:
                return info
            return os.stat_result((info.st_mode, info.st_ino, info.st_dev + 7, *tuple(info)[3:]))
        return mock.patch.object(history_module, "identity_stat", fake)

    def test_helper_rule(self):
        state, root, _ = self.fixture("helper", self.SAME)
        info = root.stat()
        match = history_module.root_identity_matches
        self.assertTrue(match(f"{info.st_dev} {info.st_ino}", root))
        self.assertTrue(match(f"{info.st_dev + 1} {info.st_ino}", root))
        self.assertFalse(match(f"{info.st_dev + 1} {info.st_ino + 1}", root))
        self.assertFalse(match(f"{info.st_dev} {info.st_ino + 1}", root))
        self.assertFalse(match("garbage", root))
        self.assertFalse(match("", root))
        with self.seam_other_device(root):
            self.assertFalse(match(f"{info.st_dev + 1} {info.st_ino}", root))
        replaced = root.with_name("replaced")
        replaced.mkdir()
        self.assertFalse(match(f"{info.st_dev + 1} {info.st_ino}", replaced))
        link = root.with_name("link")
        link.symlink_to(root, target_is_directory=True)
        self.assertFalse(match(f"{info.st_dev} {info.st_ino}", link))

    def catalog_args(self, state):
        return type("Args", (), {"source_root": str(self.source_root), "state_home": str(state), "legacy_state_home": None})()

    def test_catalog_quarantine_site(self):
        for label, recorded, seam, reason in (("renumbered", self.RENUMBERED, False, None),
                                              ("inode", self.OTHER_INODE, False, "root_inode_mismatch"),
                                              ("device", self.RENUMBERED, True, "root_inode_mismatch")):
            with self.subTest(label):
                state, root, _ = self.fixture("catalog-" + label, recorded)
                with (self.seam_other_device(root) if seam else contextlib.nullcontext()):
                    result = history_module.catalog_value(self.catalog_args(state))
                self.assertEqual([w["reason"] for w in result["warnings"]], [reason] if reason else [])

    def test_provenance_site(self):
        for label, recorded, seam, ok in (("renumbered", self.RENUMBERED, False, True),
                                          ("inode", self.OTHER_INODE, False, False),
                                          ("device", self.RENUMBERED, True, False)):
            with self.subTest(label):
                state, root, _ = self.fixture("prov-" + label, recorded)
                args = types.SimpleNamespace(isolation_id=ISOLATION_ID, runtime_revision=RUNTIME_REVISION,
                                             source_root=str(self.source_root), source=str(root / ".harness/codex"),
                                             catalog=str(state / "native-history"))
                with (self.seam_other_device(root) if seam else contextlib.nullcontext()):
                    if ok:
                        history_module.validate_provenance(args)
                    else:
                        with self.assertRaisesRegex(history_module.Refusal, "unverified Native root inode"):
                            history_module.validate_provenance(args)

    def protect(self, state):
        seen = []
        def fake_snapshot(args):
            Path(args.receipt).write_text(json.dumps({"snapshot_id": "a" * 64}))
            print(json.dumps({"snapshot_id": "a" * 64}))
        def fake_verify(args):
            seen.append(True)
        args = type("Args", (), {"state_home": str(state), "source_root": str(self.source_root), "codex_bin": "/bin/true"})()
        with mock.patch.object(history_module, "snapshot", fake_snapshot), mock.patch.object(history_module, "verify", fake_verify), \
                contextlib.redirect_stdout(io.StringIO()):
            history_module.protect_pool(args)
        return seen

    def test_protect_pool_validation_and_under_lease_sites(self):
        for label, recorded, seam, ok in (("renumbered", self.RENUMBERED, False, True),
                                          ("inode", self.OTHER_INODE, False, False),
                                          ("device", self.RENUMBERED, True, False)):
            with self.subTest(label):
                state, root, _ = self.fixture("pool-" + label, recorded)
                with (self.seam_other_device(root) if seam else contextlib.nullcontext()):
                    if ok:
                        self.assertEqual(self.protect(state), [True])
                    else:
                        with self.assertRaisesRegex(history_module.Refusal, "session root inode mismatch"):
                            self.protect(state)

    def test_under_lease_recheck_site(self):
        state, root, record = self.fixture("pool-race", self.RENUMBERED)
        real = history_module.runtime_lease
        @contextlib.contextmanager
        def swap(path):
            with real(path):
                (record / "root-inode").write_text(self.OTHER_INODE(root.stat()))
                yield
        with mock.patch.object(history_module, "runtime_lease", swap):
            with self.assertRaisesRegex(history_module.Refusal, "Native candidate changed"):
                self.protect(state)

    def test_tomb_recovery_site(self):
        for label, recorded, seam, ok in (("renumbered", self.RENUMBERED, False, True),
                                          ("inode", self.OTHER_INODE, False, False),
                                          ("device", self.RENUMBERED, True, False)):
            with self.subTest(label):
                state, root, record = self.fixture("tomb-" + label, self.SAME)
                tomb = root.with_name(".retired-" + ISOLATION_ID + "-1")
                root.rename(tomb)
                (record / "root-inode").write_text(recorded(tomb.stat()))
                with (self.seam_other_device(tomb) if seam else contextlib.nullcontext()):
                    if ok:
                        self.protect(state)
                        self.assertTrue(root.is_dir() and not tomb.exists())
                    else:
                        with self.assertRaisesRegex(history_module.Refusal, "Native tomb binding mismatch"):
                            self.protect(state)


class CatalogCostTest(unittest.TestCase):
    """D3: an exact-id catalog equals the full catalog's target row; inventory is computed once."""

    setUp = CodexHistoryTest.setUp
    _make_home = CodexHistoryTest._make_home
    _make_auxiliary_db = CodexHistoryTest._make_auxiliary_db
    _make_state_db = CodexHistoryTest._make_state_db
    _make_history_db = CodexHistoryTest._make_history_db
    run_history = CodexHistoryTest.run_history

    def hide_canonical(self):
        shutil.move(self.home, self.base / ("hidden-canonical-" + uuid_hex()))

    def iso_home(self, state, identifier, native=None):
        root, record = state / "worktrees" / identifier, state / "sessions" / identifier
        home = root / ".harness/codex"
        shutil.copytree(self.home, home, symlinks=True)
        if native:
            rollout = next(path for path in (home / "sessions").rglob("*.jsonl") if "session_meta" in path.read_text())
            value = json.loads(rollout.read_text().splitlines()[0]); value["payload"]["id"] = native
            rollout.write_text(json.dumps(value) + "\n")
        record.mkdir(parents=True)
        for name, value in (("source-root", str(self.source_root)), ("session-root", str(root)),
                            ("root-inode", history_module.root_inode(root)), ("runtime.lock", ""),
                            ("lease-v1", "1\n"), ("journal", "state=CLOSED\nidentity=\nheartbeat=2026-10-10T00:00:00Z\n")):
            (record / name).write_text(value)
        return root, record, home

    def snap(self, state, identifier, home, record):
        return json.loads(self.run_history(
            "snapshot", "--source", str(home), "--catalog", str(state / "native-history"),
            "--receipt", str(record / ("snap-" + uuid_hex() + ".json")), "--source-root", str(self.source_root),
            "--isolation-id", identifier, "--runtime-revision", RUNTIME_REVISION).stdout)

    def args(self, state, native=None):
        values = {"source_root": str(self.source_root), "state_home": str(state), "legacy_state_home": None}
        if native:
            values["native_id"] = native
        return type("Args", (), values)()

    A, B = "11111111-2222-4333-8444-55555555555a", "11111111-2222-4333-8444-55555555555b"

    def scenario_live_and_archive(self, state):
        root, record, home = self.iso_home(state, self.A)
        self.snap(state, self.A, home, record)
        self.hide_canonical()

    def scenario_held_ids(self, state):
        root, record, home = self.iso_home(state, self.A)
        (record / "root-inode").write_text("0 0\n")

    def scenario_incomplete_hints(self, state):
        self.scenario_held_ids(state)
        for path in (state / "worktrees" / self.A / ".harness/codex/sessions").rglob("*.jsonl"):
            path.rename(path.with_name("rollout-no-identifier.jsonl"))
            break

    def scenario_restored_lineage(self, state):
        first = json.loads(self.run_history("prepare", "--source-root", str(self.source_root),
                           "--state-home", str(state), "--native-id", NATIVE_THREAD_ID).stdout)
        root, record = state / "worktrees" / self.B, state / "sessions" / self.B
        (root / ".harness/codex").mkdir(parents=True); record.mkdir(parents=True)
        for name, value in (("source-root", str(self.source_root)), ("session-root", str(root)),
                            ("root-inode", history_module.root_inode(root)), ("runtime.lock", ""),
                            ("lease-v1", "1\n"), ("journal", "state=CLOSED\nidentity=\nheartbeat=2026-10-10T00:00:00Z\n")):
            (record / name).write_text(value)
        self.run_history("restore", "--catalog", str(state / "native-history"), "--snapshot", first["snapshot_id"],
                         "--destination", str(root / ".harness/codex"), "--receipt", str(record / "native-history-restore.json"),
                         "--native-id", NATIVE_THREAD_ID, "--origin-runtime", first["origin_runtime"])

    def scenario_imported_suppression(self, state):
        child = "22222222-2222-4333-8444-555555555556"
        self.iso_home(state, self.A)
        new_root, new_record, new_home = self.iso_home(state, self.B)
        rollout = next(path for path in (new_home / "sessions").rglob("*.jsonl") if NATIVE_THREAD_ID in path.name)
        (rollout.parent / ("rollout-child-" + child + ".jsonl")).write_text(
            json.dumps({"type": "session_meta", "payload": {"id": child}}) + "\n")
        ledger = new_record / "restored-native-sessions"
        ledger.write_text(json.dumps({"schema": 1, "session_id": self.B, "source_root": str(self.source_root.resolve()),
            "imports": [{"runtime": "codex", "native_id": NATIVE_THREAD_ID, "source_owner": self.A}]}, sort_keys=True) + "\n")
        ledger.chmod(0o600)
        self.hide_canonical()

    def scenario_duplicate_live(self, state):
        self.iso_home(state, self.A)
        self.iso_home(state, self.B)
        self.hide_canonical()

    def scenario_duplicate_archive(self, state):
        for identifier in (self.A, self.B):
            root, record, home = self.iso_home(state, identifier)
            self.snap(state, identifier, home, record)
            shutil.move(root, self.base / ("moved-" + identifier))
        self.hide_canonical()

    SCENARIOS = ("live_and_archive", "held_ids", "incomplete_hints", "restored_lineage",
                 "imported_suppression", "duplicate_live", "duplicate_archive")

    def test_filtered_catalog_gives_the_complete_target_row_of_the_full_catalog(self):
        for name in self.SCENARIOS:
            for target in (NATIVE_THREAD_ID, NATIVE_THREAD_ID.upper()):
                with self.subTest(scenario=name, target=target):
                    state = self.base / ("scenario-" + name)
                    if not state.exists():
                        self._make_home()
                        getattr(self, "scenario_" + name)(state)
                    full = history_module.catalog_value(self.args(state))
                    wanted = [row for row in full["entries"] if row["native_id"].lower() == NATIVE_THREAD_ID]
                    self.assertTrue(wanted, name)
                    filtered = history_module.catalog_value(self.args(state, target))
                    self.assertEqual(filtered["entries"], wanted)
                    self.assertEqual(filtered["warnings"], full["warnings"])
                    self.assertEqual(filtered["schema_version"], 1)
                    cli = json.loads(self.run_history("catalog", "--source-root", str(self.source_root),
                                                      "--state-home", str(state), "--native-id", target).stdout)
                    self.assertEqual(cli, filtered)
            self.assertEqual(self.EXPECTED[name], sorted((r["status"], r["reason"], bool(r["snapshot_id"])) for r in wanted), name)

    EXPECTED = {
        "live_and_archive": [("live", None, True)],
        "held_ids": [("ambiguous", "unverified_native_owner", False)],
        "incomplete_hints": [("ambiguous", "unverified_native_owner_hints_incomplete", False)],
        "restored_lineage": [("live", None, False)],
        "imported_suppression": [("live", None, False)],
        "duplicate_live": [("ambiguous", "duplicate_live_owner", False)],
        "duplicate_archive": [("ambiguous", "duplicate_archive_owner", True)],
    }

    def multi_row_live_home_with_archive(self, name, extra=3):
        state = self.base / name
        root, record, home = self.iso_home(state, self.A)
        directory = next((home / "sessions").rglob("*.jsonl")).parent
        for index in range(extra):
            identifier = "33333333-2222-4333-8444-55555555550%d" % index
            (directory / ("rollout-extra-" + identifier + ".jsonl")).write_text(
                json.dumps({"type": "session_meta", "payload": {"id": identifier}}) + "\n")
        self.snap(state, self.A, home, record)
        self.hide_canonical()
        return state, home

    def counted(self, home, after_call=None):
        calls = []
        real = history_module.source_inventory
        def wrapper(source, children):
            result = real(source, children)
            if Path(source) == home:
                calls.append(1)
                if after_call:
                    after_call(len(calls))
            return result
        return calls, mock.patch.object(history_module, "source_inventory", wrapper)

    def test_inventory_is_computed_once_per_home_for_many_rows(self):
        state, home = self.multi_row_live_home_with_archive("inventory-once")
        calls, patch = self.counted(home)
        with patch:
            result = history_module.catalog_value(self.args(state))
        self.assertGreaterEqual(len([r for r in result["entries"] if r["snapshot_id"]]), 4)
        self.assertEqual(len(calls), 1)

    def test_inventory_is_recomputed_when_a_nested_file_changes_between_rows(self):
        state, home = self.multi_row_live_home_with_archive("inventory-nested")
        nested = next((home / "sessions").rglob("rollout-extra-*.jsonl"))
        def mutate(count):
            if count == 1:
                nested.write_text(nested.read_text() + "{}\n")
        calls, patch = self.counted(home, mutate)
        with patch:
            history_module.catalog_value(self.args(state))
        self.assertEqual(len(calls), 2)

    def test_inventory_is_recomputed_when_only_mtime_changes_between_rows(self):
        state, home = self.multi_row_live_home_with_archive("inventory-mtime")
        nested = next((home / "sessions").rglob("rollout-extra-*.jsonl"))
        def mutate(count):
            if count == 1:
                info = nested.stat()
                os.utime(nested, ns=(info.st_atime_ns, info.st_mtime_ns + 5_000_000_000))
        calls, patch = self.counted(home, mutate)
        with patch:
            history_module.catalog_value(self.args(state))
        self.assertEqual(len(calls), 2)

    def signature_walks(self, home):
        walks = []
        real = history_module.inventory_signature
        def wrapper(path, children):
            if Path(path) == home:
                walks.append(1)
            return real(path, children)
        return walks, mock.patch.object(history_module, "inventory_signature", wrapper)

    def test_signature_is_walked_twice_per_home_however_many_rows(self):
        state, home = self.multi_row_live_home_with_archive("signature-twice", 6)
        walks, patch = self.signature_walks(home)
        with patch:
            result = history_module.catalog_value(self.args(state))
        self.assertGreaterEqual(len([r for r in result["entries"] if r["snapshot_id"]]), 7)
        self.assertEqual(len(walks), 2)

    def test_change_between_first_and_end_signature_redoes_once_and_keeps_rows(self):
        state, home = self.multi_row_live_home_with_archive("signature-redo-once")
        baseline = history_module.catalog_value(self.args(state))
        nested = next((home / "sessions").rglob("rollout-extra-*.jsonl"))
        calls, patch = self.counted(home, lambda count: nested.write_text(nested.read_text() + "{}\n") if count == 1 else None)
        with patch:
            result = history_module.catalog_value(self.args(state))
        self.assertEqual(len(calls), 2)
        self.assertFalse([r for r in result["entries"] if r["reason"] == "source_changed_during_catalog"])
        self.assertEqual([r["native_id"] for r in result["entries"]], [r["native_id"] for r in baseline["entries"]])
        self.assertEqual(len(result["entries"]), len(baseline["entries"]))

    def test_change_on_the_redo_too_holds_the_rows_that_used_the_home(self):
        state, home = self.multi_row_live_home_with_archive("signature-redo-twice")
        nested = next((home / "sessions").rglob("rollout-extra-*.jsonl"))
        calls, patch = self.counted(home, lambda count: nested.write_text(nested.read_text() + "{}\n"))
        with patch:
            result = history_module.catalog_value(self.args(state))
        self.assertEqual(len(calls), 2)
        held = [r for r in result["entries"] if r["home"] == str(home)]
        self.assertGreaterEqual(len(held), 4)
        for row in held:
            self.assertEqual((row["status"], row["reason"]), ("ambiguous", "source_changed_during_catalog"))

    def test_home_removed_between_first_and_end_signature_is_held_without_traceback(self):
        state, home = self.multi_row_live_home_with_archive("signature-home-removed")
        calls, patch = self.counted(home, lambda count: shutil.rmtree(home) if count == 1 else None)
        with patch:
            result = history_module.catalog_value(self.args(state))
        held = [r for r in result["entries"] if r["home"] == str(home)]
        self.assertTrue(held)
        for row in held:
            self.assertEqual((row["status"], row["reason"]), ("ambiguous", "source_changed_during_catalog"))


class OriginStateHomeTest(unittest.TestCase):
    """origin_state_home names the one configured state home that owns a row's origin record."""

    SOURCE = Path("/work/src")
    CURRENT = Path("/state/current")
    LEGACY = Path("/state/legacy")

    def origin(self, row, pools=None, legacy="default"):
        pools = pools if pools is not None else [self.CURRENT, self.LEGACY]
        legacy = self.LEGACY if legacy == "default" else legacy
        return history_module.origin_state_home(row, self.SOURCE, pools, legacy)

    def live(self, pool, identifier=ISOLATION_ID):
        return {"status": "live", "isolation_id": identifier,
                "home": str(pool / "worktrees" / identifier / ".harness" / "codex")}

    def test_live_row_maps_to_its_pool(self):
        self.assertEqual(self.origin(self.live(self.CURRENT)), str(self.CURRENT))
        self.assertEqual(self.origin(self.live(self.LEGACY)), str(self.LEGACY))

    def test_live_row_in_an_unconfigured_pool_grants_nothing(self):
        self.assertEqual(self.origin(self.live(Path("/state/other"))), "")

    def test_snapshot_row_uses_the_manifest_source_not_the_store(self):
        row = {"status": "snapshot", "isolation_id": ISOLATION_ID,
               "home": "/state/current/native-history/snapshots/x/data",
               "_source_home": str(self.live(self.LEGACY)["home"])}
        self.assertEqual(self.origin(row), str(self.LEGACY))

    def test_canonical_row_and_canonical_snapshot_map_to_the_legacy_home(self):
        canonical = {"status": "canonical", "isolation_id": "canonical",
                     "home": str(self.SOURCE / ".harness" / "codex")}
        self.assertEqual(self.origin(canonical), str(self.LEGACY))
        snapshot = {"status": "snapshot", "isolation_id": "canonical", "home": "/s/data",
                    "_source_home": str(self.SOURCE / ".harness" / "codex")}
        self.assertEqual(self.origin(snapshot), str(self.LEGACY))
        self.assertEqual(self.origin(canonical, legacy=None), "")

    def test_malformed_layouts_grant_nothing(self):
        row = self.live(self.CURRENT)
        self.assertEqual(self.origin(dict(row, isolation_id="not-a-uuid")), "")
        self.assertEqual(self.origin(dict(row, home=str(self.CURRENT / "elsewhere" / "codex"))), "")
        self.assertEqual(self.origin({"status": "live"}), "")

    def test_catalog_rows_carry_the_field_without_private_keys(self):
        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            source, state = base / "source", base / "state"
            home = source / ".harness" / "codex" / "sessions" / "2026" / "10" / "10"
            home.mkdir(parents=True)
            (home / ("rollout-2026-10-10T01-02-03-%s.jsonl" % NATIVE_THREAD_ID)).write_text(
                json.dumps({"type": "session_meta", "payload": {"id": NATIVE_THREAD_ID}}) + "\n")
            state.mkdir()
            args = types.SimpleNamespace(source_root=str(source), state_home=str(state),
                                         legacy_state_home=str(base / "legacy"), native_id=None)
            rows = history_module.catalog_value(args)["entries"]
            self.assertEqual([row["origin_state_home"] for row in rows], [str(base / "legacy")])
            self.assertFalse([key for row in rows for key in row if key.startswith("_")])


if __name__ == "__main__":
    unittest.main()
