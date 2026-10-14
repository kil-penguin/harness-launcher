"""Allowlisted native Codex plugins follow the canonical home into isolated homes."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import tomllib
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

SID = "11111111-1111-4111-8111-111111111111"
PLUGIN_ID = "glider@glider"
MARKET = {"source_type": "local", "source": None}


def load_module(name="codex_plugin_inherit_test"):
    spec = importlib.util.spec_from_file_location(name, ROOT / "bin/codex-plugin-inherit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_package(cache, plugin, version, extra=None):
    package = cache / plugin / version
    (package / ".codex-plugin").mkdir(parents=True)
    (package / ".codex-plugin/plugin.json").write_text(json.dumps({"name": plugin, "version": version, "skills": "./skills/"}))
    (package / "hooks").mkdir()
    (package / "hooks/hooks.json").write_text(json.dumps({"hooks": {"SessionStart": [{"matcher": "startup", "hooks": [{"type": "command", "command": "\"${PLUGIN_ROOT}/hooks/session-start\" codex", "timeout": 5}]}]}}))
    (package / "hooks/session-start").write_text("#!/bin/sh\nexit 0\n")
    (package / "hooks/session-start").chmod(0o755)
    (package / "skills/demo").mkdir(parents=True)
    (package / "skills/demo/SKILL.md").write_text("---\nname: demo\ndescription: demo\n---\n")
    for relative, content in (extra or {}).items():
        (package / relative).parent.mkdir(parents=True, exist_ok=True)
        (package / relative).write_text(content)
    return package


def tree(path):
    result = {}
    for item in sorted(path.rglob("*")):
        relative = str(item.relative_to(path))
        if item.is_symlink():
            result[relative] = ("link", os.readlink(item))
        elif item.is_dir():
            result[relative] = ("dir",)
        else:
            result[relative] = ("file", bool(item.stat().st_mode & 0o111), item.read_bytes())
    return result


class Layout:
    """Physical broker-bound session beside a canonical source checkout."""

    def __init__(self, case, plugins=("glider",), allow=(PLUGIN_ID,), session_allow=None):
        self.case = case
        self.tmp = Path(tempfile.mkdtemp(prefix="plugin-inherit.")).resolve()
        case.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.marketplace = self.tmp / "marketplace"
        self.marketplace.mkdir()
        self.source = self.tmp / "source"
        self.state = self.tmp / "state"
        self.target = self.state / "worktrees" / SID
        self.record = self.state / "sessions" / SID
        self.source_home = self.source / ".harness/codex"
        self.home = self.target / ".harness/codex"
        self.source_home.mkdir(parents=True)
        self.home.mkdir(parents=True)
        self.record.mkdir(parents=True)
        self.manifest(self.source, allow)
        self.manifest(self.target, allow if session_allow is None else session_allow)
        tables = ["model = \"canonical\"", "", "[marketplaces.glider]", "source_type = \"local\"",
                  "source = " + json.dumps(str(self.marketplace)), ""]
        for plugin in plugins:
            tables += [f"[plugins.\"{plugin}@glider\"]", "enabled = true", ""]
            write_package(self.source_home / "plugins/cache/glider", plugin, "0.1.35")
        tables += ["[hooks.state.\"glider@glider:hooks/hooks.json:session_start:0:0\"]", "enabled = true",
                   "trusted_hash = \"sha256:" + "a" * 64 + "\"", ""]
        (self.source_home / "config.toml").write_text("\n".join(tables))
        (self.source_home / "auth.json").write_text("{\"token\": \"secret\"}")
        (self.source_home / ".glider").mkdir()
        (self.source_home / ".glider/last-agents-sync").write_text("1")
        (self.home / "config.toml").write_text("model = \"session\"\n")
        for name, value in {"source-root": str(self.source), "session-root": str(self.target),
                            "base-sha": "b" * 40,
                            "root-inode": f"{self.target.stat().st_dev} {self.target.stat().st_ino}",
                            "journal": "state=OPEN"}.items():
            (self.record / name).write_text(value + "\n")
        patch = mock.patch.dict(os.environ, {
            "HARNESS_SESSION_ID": SID, "HARNESS_SESSION_STATE_HOME": str(self.state),
            "HARNESS_SESSION_ROOT": str(self.target), "HARNESS_SOURCE_ROOT": str(self.source)})
        patch.start()
        case.addCleanup(patch.stop)

    @staticmethod
    def manifest(root, allow):
        (root / "config").mkdir(parents=True, exist_ok=True)
        payload = {"schema_version": 1, "repo": "fixture"}
        if allow is not None:
            payload["runtime_audit"] = {"native_codex_plugins": list(allow)}
        (root / "config/codex-surface.json").write_text(json.dumps(payload))

    def source_config(self, edit):
        path = self.source_home / "config.toml"
        path.write_text(edit(path.read_text()))

    def config(self):
        return tomllib.loads((self.home / "config.toml").read_text())

    def cache(self, plugin="glider"):
        return self.home / "plugins/cache/glider" / plugin


class InheritTests(unittest.TestCase):
    def setUp(self):
        self.module = load_module()

    def ensure(self, layout):
        return self.module.ensure(layout.home)

    def assertNotInherited(self, layout, before=None):
        config = layout.config()
        self.assertNotIn(PLUGIN_ID, config.get("plugins", {}))
        self.assertFalse(layout.cache().exists())
        if before is not None:
            self.assertEqual(before, (layout.home / "config.toml").read_bytes())

    def test_inherits_only_package_and_two_tables(self):
        layout = Layout(self)
        self.ensure(layout)
        config = layout.config()
        self.assertEqual(config["marketplaces"], {"glider": {"source_type": "local", "source": str(layout.marketplace)}})
        self.assertEqual(config["plugins"], {PLUGIN_ID: {"enabled": True}})
        self.assertNotIn("hooks", config, "plugin hook trust must stay with native /hooks review")
        self.assertEqual(config["model"], "session")
        self.assertEqual(tree(layout.source_home / "plugins/cache/glider/glider"), tree(layout.cache()))
        self.assertTrue(os.access(layout.cache() / "0.1.35/hooks/session-start", os.X_OK))
        self.assertEqual(stat.S_IMODE(layout.cache().stat().st_mode), 0o755)
        for runtime_state in ("auth.json", ".glider"):
            self.assertFalse((layout.home / runtime_state).exists(), runtime_state)
        first = (layout.home / "config.toml").read_bytes()
        inode = (layout.cache() / "0.1.35").stat().st_ino
        self.ensure(layout)
        self.assertEqual(first, (layout.home / "config.toml").read_bytes())
        self.assertEqual(inode, (layout.cache() / "0.1.35").stat().st_ino)

    def test_missing_or_unsafe_evidence_inherits_nothing(self):
        def not_installed(layout):
            shutil.rmtree(layout.source_home / "plugins")
        def disabled(layout):
            layout.source_config(lambda text: text.replace("enabled = true\n\n[hooks", "enabled = false\n\n[hooks"))
        def extra_plugin_key(layout):
            layout.source_config(lambda text: text.replace("[plugins.\"glider@glider\"]\n", "[plugins.\"glider@glider\"]\nextra = 1\n"))
        def remote_marketplace(layout):
            layout.source_config(lambda text: text.replace("source_type = \"local\"", "source_type = \"git\""))
        def extra_marketplace_key(layout):
            layout.source_config(lambda text: text.replace("source_type = \"local\"", "source_type = \"local\"\nref = \"main\""))
        def relative_marketplace(layout):
            layout.source_config(lambda text: text.replace(json.dumps(str(layout.marketplace)), "\"marketplace\""))
        def missing_marketplace(layout):
            shutil.rmtree(layout.marketplace)
        def two_versions(layout):
            write_package(layout.source_home / "plugins/cache/glider", "glider", "0.1.36")
        def symlink_in_package(layout):
            (layout.source_home / "plugins/cache/glider/glider/0.1.35/skills/link").symlink_to("/etc")
        def symlinked_version(layout):
            cache = layout.source_home / "plugins/cache/glider/glider"
            (cache / "0.1.35").rename(layout.tmp / "outside")
            (cache / "0.1.35").symlink_to(layout.tmp / "outside")
        def manifest_mismatch(layout):
            path = layout.source_home / "plugins/cache/glider/glider/0.1.35/.codex-plugin/plugin.json"
            path.write_text(json.dumps({"name": "glider", "version": "0.1.36"}))
        def writable_package_file(layout):
            (layout.source_home / "plugins/cache/glider/glider/0.1.35/skills/demo/SKILL.md").chmod(0o666)
        def absent_from_canonical_policy(layout):
            Layout.manifest(layout.source, [])
        def absent_from_session_policy(layout):
            Layout.manifest(layout.target, [])
        def invalid_policy(layout):
            Layout.manifest(layout.target, [PLUGIN_ID, PLUGIN_ID])
        def missing_policy(layout):
            Layout.manifest(layout.source, None)
        def no_broker_record(layout):
            shutil.rmtree(layout.record)
        def closed_journal(layout):
            (layout.record / "journal").write_text("state=OPEN\nstate=DELIVERED\n")
        def other_source(layout):
            (layout.record / "source-root").write_text(str(layout.target) + "\n")
        for tweak in (not_installed, disabled, extra_plugin_key, remote_marketplace, extra_marketplace_key,
                      relative_marketplace, missing_marketplace, two_versions, symlink_in_package,
                      symlinked_version, manifest_mismatch, writable_package_file,
                      absent_from_canonical_policy, absent_from_session_policy, invalid_policy,
                      missing_policy, no_broker_record, closed_journal, other_source):
            with self.subTest(case=tweak.__name__):
                layout = Layout(self)
                tweak(layout)
                before = (layout.home / "config.toml").read_bytes()
                self.ensure(layout)
                self.assertNotInherited(layout, before)

    def test_launcher_managed_marketplace_is_never_inherited(self):
        layout = Layout(self, allow=("computer-use@openai-bundled",))
        before = (layout.home / "config.toml").read_bytes()
        self.ensure(layout)
        self.assertEqual(before, (layout.home / "config.toml").read_bytes())
        self.assertFalse((layout.home / "plugins").exists())

    def test_installed_plugin_outside_allowlist_stays_behind(self):
        layout = Layout(self, plugins=("glider", "other"))
        self.ensure(layout)
        self.assertEqual(set(layout.config()["plugins"]), {PLUGIN_ID})
        self.assertFalse(layout.cache("other").exists())

    def test_marketplace_shared_by_two_allowlisted_plugins_is_written_once(self):
        layout = Layout(self, plugins=("glider", "extra"), allow=(PLUGIN_ID, "extra@glider"))
        self.ensure(layout)
        config = layout.config()
        self.assertEqual(set(config["plugins"]), {PLUGIN_ID, "extra@glider"})
        self.assertEqual(list(config["marketplaces"]), ["glider"])
        self.assertTrue(layout.cache("extra").is_dir())

    def test_existing_session_decision_wins(self):
        layout = Layout(self)
        (layout.home / "config.toml").write_text("model = \"session\"\n\n[plugins.\"glider@glider\"]\nenabled = false\n")
        before = (layout.home / "config.toml").read_bytes()
        self.ensure(layout)
        self.assertEqual(before, (layout.home / "config.toml").read_bytes())
        self.assertFalse(layout.cache().exists())

    def test_matching_session_marketplace_is_reused(self):
        layout = Layout(self)
        (layout.home / "config.toml").write_text("model = \"session\"\n\n[marketplaces.glider]\nsource_type = \"local\"\nsource = "
                                                 + json.dumps(str(layout.marketplace)) + "\n")
        self.ensure(layout)
        self.assertEqual(layout.config()["plugins"], {PLUGIN_ID: {"enabled": True}})

    def test_conflicting_session_marketplace_is_not_overwritten(self):
        layout = Layout(self)
        (layout.home / "config.toml").write_text("model = \"session\"\n\n[marketplaces.glider]\nsource_type = \"local\"\nsource = \"/elsewhere\"\n")
        before = (layout.home / "config.toml").read_bytes()
        self.ensure(layout)
        self.assertNotInherited(layout, before)

    def test_later_canonical_install_is_inherited_on_next_preparation(self):
        layout = Layout(self)
        package = layout.source_home / "plugins"
        package.rename(layout.tmp / "parked")
        self.ensure(layout)
        self.assertNotInherited(layout)
        (layout.tmp / "parked").rename(package)
        self.ensure(layout)
        self.assertEqual(layout.config()["plugins"], {PLUGIN_ID: {"enabled": True}})

    def test_differing_session_cache_is_left_untouched_with_remediation(self):
        layout = Layout(self)
        stale = write_package(layout.home / "plugins/cache/glider", "glider", "0.1.30")
        (stale / "leftover").write_text("old")
        before = tree(layout.cache())
        notices = self.ensure(layout)
        self.assertEqual(before, tree(layout.cache()))
        self.assertNotIn("plugins", layout.config())
        self.assertTrue(any(str(layout.cache()) in notice for notice in notices), notices)

    def test_success_leaves_no_staging_and_keeps_foreign_staging_names(self):
        layout = Layout(self)
        foreign = layout.home / "plugins/.harness-inherit-foreign"
        foreign.mkdir(parents=True)
        (foreign / "keep").write_text("foreign")
        (layout.home / "plugins/.harness-inherit-link").symlink_to(layout.tmp)
        self.ensure(layout)
        names = sorted(p.name for p in (layout.home / "plugins").iterdir())
        self.assertEqual(names, [".harness-inherit-foreign", ".harness-inherit-link", ".harness-inherited.json", "cache"])
        self.assertEqual((foreign / "keep").read_text(), "foreign")

    def test_concurrent_native_install_wins_and_is_untouched(self):
        layout = Layout(self)
        native = {}
        def install(market_fd):
            package = write_package(layout.home / "plugins/cache/glider", "glider", "0.2.0")
            native["tree"] = tree(package.parent)
            with open(layout.home / "config.toml", "a") as stream:
                stream.write("\n[plugins.\"glider@glider\"]\nenabled = true\n")
            native["config"] = (layout.home / "config.toml").read_bytes()
        with mock.patch.object(self.module, "_before_install", install):
            self.ensure(layout)
        staged = [p for p in (layout.home / "plugins").iterdir() if p.name.startswith(".harness-inherit-")]
        self.assertEqual([stat.S_IMODE(p.stat().st_mode) for p in staged], [0o700], "failed attempt keeps its private staging")
        self.assertEqual(native["tree"], tree(layout.cache()))
        self.assertEqual(native["config"], (layout.home / "config.toml").read_bytes())
        self.assertNotIn("hooks", layout.config())

    def test_missing_exclusive_rename_fails_closed(self):
        layout = Layout(self)
        with mock.patch.object(self.module, "_flagged_rename", side_effect=self.module.Skip("exclusive rename unavailable")):
            notices = self.ensure(layout)
        self.assertNotInherited(layout)
        self.assertTrue(any("exclusive rename unavailable" in notice for notice in notices), notices)
        self.assertFalse(any(p.name.startswith(".harness-inherit-") for p in (layout.home / "plugins").iterdir()))

    def test_native_uninstall_is_not_reversed(self):
        layout = Layout(self)
        self.ensure(layout)
        shutil.rmtree(layout.cache())
        (layout.home / "config.toml").write_text("model = \"session\"\n\n[marketplaces.glider]\nsource_type = \"local\"\nsource = "
                                                 + json.dumps(str(layout.marketplace)) + "\n")
        before = (layout.home / "config.toml").read_bytes()
        notices = self.ensure(layout)
        self.assertEqual(before, (layout.home / "config.toml").read_bytes())
        self.assertFalse(layout.cache().exists())
        self.assertTrue(any("removed in this session" in notice for notice in notices), notices)

    def test_unsafe_record_only_suppresses(self):
        def forged_outside_allowlist(record):
            record.write_text(json.dumps({"schema_version": 1, "inherited": {"other@glider": "1.0.0"}}))
        def malformed(record):
            record.write_text("{")
        def directory(record):
            record.mkdir()
        def symlink(record):
            target = record.parent.parent / "elsewhere.json"
            target.write_text(json.dumps({"schema_version": 1, "inherited": {}}))
            record.symlink_to(target)
        for tweak, inherited in ((forged_outside_allowlist, True), (malformed, False), (directory, False), (symlink, False)):
            with self.subTest(case=tweak.__name__):
                layout = Layout(self)
                record = layout.home / "plugins/.harness-inherited.json"
                record.parent.mkdir()
                tweak(record)
                self.ensure(layout)
                if inherited:
                    self.assertEqual(layout.config()["plugins"], {PLUGIN_ID: {"enabled": True}})
                    saved = json.loads(record.read_text())["inherited"]
                    self.assertEqual(saved, {"other@glider": "1.0.0", PLUGIN_ID: "0.1.35"})
                else:
                    self.assertNotInherited(layout)

    def test_concurrent_record_writer_is_preserved(self):
        layout = Layout(self)
        record = layout.home / "plugins/.harness-inherited.json"
        def concurrent(plugins_fd):
            record.write_text("concurrent")
        with mock.patch.object(self.module, "_before_record", concurrent):
            notices = self.ensure(layout)
        self.assertEqual(record.read_text(), "concurrent")
        self.assertEqual(layout.config()["plugins"], {PLUGIN_ID: {"enabled": True}})
        self.assertTrue(any("record changed concurrently" in notice for notice in notices), notices)

    def test_unverifiable_config_swap_restores_original(self):
        layout = Layout(self)
        config = layout.home / "config.toml"
        before, inode = config.read_bytes(), config.stat().st_ino
        real = self.module._identity
        def failing(parent, name):
            if name.startswith(".config.toml."):
                raise OSError("injected")
            return real(parent, name)
        with mock.patch.object(self.module, "_identity", failing):
            self.ensure(layout)
        self.assertEqual((before, inode), (config.read_bytes(), config.stat().st_ino))
        self.assertEqual([p.name for p in layout.home.iterdir() if p.name.startswith(".config.toml")], [])

    def test_failed_put_back_keeps_the_replaced_config(self):
        layout = Layout(self)
        before = (layout.home / "config.toml").read_bytes()
        real_rename = self.module._rename
        swaps = []
        def rename(src_fd, src, dst_fd, dst, flags):
            if flags == self.module.SWAP:
                swaps.append(src)
                if len(swaps) == 2:
                    raise OSError("put-back failed")
            return real_rename(src_fd, src, dst_fd, dst, flags)
        with mock.patch.object(self.module, "_identity", return_value=None), \
                mock.patch.object(self.module, "_rename", rename):
            self.ensure(layout)
        kept = [p for p in layout.home.iterdir() if p.name.startswith(".config.toml.")]
        self.assertEqual([p.read_bytes() for p in kept], [before])

    def test_unverifiable_record_swap_restores_original(self):
        layout = Layout(self)
        record = layout.home / "plugins/.harness-inherited.json"
        record.parent.mkdir()
        record.write_text(json.dumps({"schema_version": 1, "inherited": {"other@glider": "1.0.0"}}))
        before, inode = record.read_bytes(), record.stat().st_ino
        real = self.module._identity
        def failing(parent, name):
            if name.startswith(".harness-inherited.json."):
                raise OSError("injected")
            return real(parent, name)
        with mock.patch.object(self.module, "_identity", failing):
            self.ensure(layout)
        self.assertEqual((before, inode), (record.read_bytes(), record.stat().st_ino))
        self.assertEqual([p.name for p in record.parent.iterdir() if p.name.startswith(".harness-inherited.json.")], [])

    def test_root_replaced_after_verification_receives_no_write(self):
        layout = Layout(self)
        real = self.module._trust()
        moved = layout.tmp / "moved-root"
        def broker_source(published):
            result = real.broker_source(published)
            layout.target.rename(moved)
            shutil.copytree(moved, layout.target)
            return result
        trust = mock.Mock(broker_source=broker_source, regular=real.regular)
        before = (layout.home / "config.toml").read_bytes()
        notices = self.module.ensure(layout.home, trust)
        self.assertEqual(before, (layout.home / "config.toml").read_bytes())
        self.assertEqual(before, (moved / ".harness/codex/config.toml").read_bytes())
        self.assertFalse((layout.home / "plugins").exists())
        self.assertFalse((moved / ".harness/codex/plugins").exists())
        self.assertTrue(any("session root replaced" in notice for notice in notices), notices)

    def test_crash_after_config_before_record_is_converged(self):
        layout = Layout(self)
        with mock.patch.object(self.module, "_write_record", side_effect=OSError("crash")):
            self.ensure(layout)
        self.assertEqual(layout.config()["plugins"], {PLUGIN_ID: {"enabled": True}})
        self.assertFalse((layout.home / "plugins/.harness-inherited.json").exists())
        before = (layout.home / "config.toml").read_bytes()
        self.ensure(layout)
        self.assertEqual(before, (layout.home / "config.toml").read_bytes())

    def test_canonical_upgrade_after_post_rename_crash_skips_without_writes(self):
        layout = Layout(self)
        with mock.patch.object(self.module, "_before_swap", side_effect=OSError("crash")):
            self.ensure(layout)
        stranded = tree(layout.cache())
        shutil.rmtree(layout.source_home / "plugins/cache/glider/glider")
        write_package(layout.source_home / "plugins/cache/glider", "glider", "0.1.36")
        before = (layout.home / "config.toml").read_bytes()
        notices = self.ensure(layout)
        self.assertEqual(before, (layout.home / "config.toml").read_bytes())
        self.assertEqual(stranded, tree(layout.cache()))
        self.assertTrue(any(str(layout.cache()) in notice for notice in notices), notices)

    def test_symlinked_destination_ancestor_receives_no_write(self):
        for component in ("plugins", "plugins/cache", "plugins/cache/glider"):
            with self.subTest(component=component):
                layout = Layout(self)
                outside = layout.tmp / "outside"
                outside.mkdir()
                link = layout.home / component
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(outside, target_is_directory=True)
                before = (layout.home / "config.toml").read_bytes()
                self.ensure(layout)
                self.assertEqual(list(outside.iterdir()), [])
                self.assertEqual(before, (layout.home / "config.toml").read_bytes())

    def test_concurrent_config_write_is_retained(self):
        layout = Layout(self)
        calls = []
        def concurrent(home_fd):
            if not calls:
                with open(layout.home / "config.toml", "a") as stream:
                    stream.write("\n[projects.\"/concurrent\"]\ntrust_level = \"trusted\"\n")
            calls.append(home_fd)
        with mock.patch.object(self.module, "_before_swap", concurrent):
            self.ensure(layout)
        config = layout.config()
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(config["projects"], {"/concurrent": {"trust_level": "trusted"}})
        self.assertEqual(config["plugins"], {PLUGIN_ID: {"enabled": True}})
        self.assertEqual([p.name for p in layout.home.iterdir() if p.name.startswith(".config.toml")], [])

    def test_failed_config_publication_leaves_reusable_cache(self):
        layout = Layout(self)
        with mock.patch.object(self.module, "_before_swap", side_effect=OSError("injected")):
            self.ensure(layout)
        self.assertNotIn("plugins", layout.config())
        inode = (layout.cache() / "0.1.35").stat().st_ino
        self.ensure(layout)
        self.assertEqual(layout.config()["plugins"], {PLUGIN_ID: {"enabled": True}})
        self.assertEqual(inode, (layout.cache() / "0.1.35").stat().st_ino)


@unittest.skipUnless(Path("/usr/bin/lockf").is_file(), "requires macOS /usr/bin/lockf")
class PrepareIntegrationTests(unittest.TestCase):
    def test_isolated_prepare_inherits_and_warm_prepare_is_unchanged(self):
        import test_codex_surface as fixtures
        for policy in ("manage", "preserve"):
            with self.subTest(policy=policy):
                fixture = fixtures.PrepareIntegrationTests("runTest")
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                source = fixture.repo.resolve()
                fixture.repo = source
                (fixture.home / ".codex").mkdir()
                global_config = fixture.home / ".codex/config.toml"
                global_config.write_text("# untouched global fixture\n")
                manifest = json.loads(fixture.manifest_path.read_text())
                manifest["runtime_audit"] = {"native_codex_plugins": [PLUGIN_ID]}
                fixture.manifest_path.write_text(json.dumps(manifest))
                fixture.prepare(HARNESS_CODEX_SLACK_APPS="")
                canonical = source / ".harness/codex"
                canonical_before = (canonical / "config.toml").read_bytes()
                marketplace = fixture.tmp.resolve() / "marketplace"
                marketplace.mkdir()
                write_package(canonical / "plugins/cache/glider", "glider", "0.1.35")
                with (canonical / "config.toml").open("a") as stream:
                    stream.write("\n[marketplaces.glider]\nsource_type = \"local\"\nsource = " + json.dumps(str(marketplace))
                                 + "\n\n[plugins.\"glider@glider\"]\nenabled = true\n")
                canonical_before = (canonical / "config.toml").read_bytes()
                fixture.prepare(HARNESS_CODEX_SLACK_APPS="")
                self.assertEqual(canonical_before, (canonical / "config.toml").read_bytes(), "canonical home must stay untouched")
                state = fixture.tmp.resolve() / "launcher-state"
                target = state / "worktrees" / SID
                record = state / "sessions" / SID
                shutil.copytree(source, target, ignore=shutil.ignore_patterns(".harness"))
                record.mkdir(parents=True)
                for name, value in {"source-root": str(source), "session-root": str(target), "base-sha": "b" * 40,
                                    "root-inode": f"{target.stat().st_dev} {target.stat().st_ino}",
                                    "journal": "state=OPEN"}.items():
                    (record / name).write_text(value + "\n")
                fixture.repo = target
                env = dict(HARNESS_CODEX_SLACK_APPS="", HARNESS_SOURCE_ROOT=str(source), HARNESS_SESSION_ROOT=str(target),
                           HARNESS_SESSION_ID=SID, HARNESS_SESSION_STATE_HOME=str(state),
                           HARNESS_CODEX_GLOBAL_PLUGIN_POLICY=policy)
                fixture.prepare(**env)
                home = target / ".harness/codex"
                config = tomllib.loads((home / "config.toml").read_text())
                self.assertEqual(config["plugins"][PLUGIN_ID], {"enabled": True})
                self.assertEqual(config["marketplaces"]["glider"], {"source_type": "local", "source": str(marketplace)})
                self.assertFalse(any(key.startswith("glider@glider:") for key in config.get("hooks", {}).get("state", {})))
                self.assertEqual(tree(canonical / "plugins/cache/glider"), tree(home / "plugins/cache/glider"))
                first = (home / "config.toml").read_bytes()
                calls = fixture.counter.read_bytes()
                fixture.prepare(**env)
                self.assertEqual(first, (home / "config.toml").read_bytes(), "warm preparation rewrote inherited state")
                self.assertEqual(calls, fixture.counter.read_bytes(), "inherited plugin state invalidated the warm surface")
                manifest_path = target / "config/codex-surface.json"
                changed = json.loads(manifest_path.read_text())
                changed["repo"] = "changed"
                manifest_path.write_text(json.dumps(changed))
                fixture.prepare(**env)
                rebuilt = tomllib.loads((home / "config.toml").read_text())
                self.assertEqual(rebuilt["plugins"][PLUGIN_ID], {"enabled": True}, "cold rebuild lost inherited plugin")
                self.assertEqual(global_config.read_text(), "# untouched global fixture\n")


if __name__ == "__main__":
    unittest.main()
