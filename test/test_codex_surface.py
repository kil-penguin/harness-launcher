import hashlib
import json
import os
from pathlib import Path
import re
import signal
import shlex
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from unittest import mock
import importlib.util
from typing import Mapping

from timed_unittest import runner_from_env


ROOT = Path(__file__).resolve().parents[1]
RESOLVER = ROOT / "bin" / "codex-surface.py"
PREPARE = ROOT / "bin" / "codex-home-prepare.sh"
GLOBAL_MCP_MODULE_PATH = ROOT / "bin" / "codex_global_mcp.py"
GLOBAL_MCP_SPEC = importlib.util.spec_from_file_location(
    "codex_global_mcp_surface_test", GLOBAL_MCP_MODULE_PATH
)
assert GLOBAL_MCP_SPEC and GLOBAL_MCP_SPEC.loader
GLOBAL_MCP_MODULE = importlib.util.module_from_spec(GLOBAL_MCP_SPEC)
sys.modules[GLOBAL_MCP_SPEC.name] = GLOBAL_MCP_MODULE
GLOBAL_MCP_SPEC.loader.exec_module(GLOBAL_MCP_MODULE)
WARM_PROBE_PATH = ROOT / "bin" / "codex-surface-warm.py"
WARM_PROBE_SPEC = importlib.util.spec_from_file_location(
    "codex_surface_warm_test", WARM_PROBE_PATH
)
assert WARM_PROBE_SPEC and WARM_PROBE_SPEC.loader
WARM_PROBE_MODULE = importlib.util.module_from_spec(WARM_PROBE_SPEC)
sys.modules[WARM_PROBE_SPEC.name] = WARM_PROBE_MODULE
WARM_PROBE_SPEC.loader.exec_module(WARM_PROBE_MODULE)

COORDINATION_TIMEOUT_ENV = "HARNESS_TEST_COORDINATION_TIMEOUT_SECONDS"


def parse_coordination_timeout(env: Mapping[str, str]) -> int:
    raw = env.get(COORDINATION_TIMEOUT_ENV)
    if raw is None:
        return 30
    if not re.fullmatch(r"[0-9]+", raw):
        raise ValueError(f"{COORDINATION_TIMEOUT_ENV} must be an integer from 1 to 120")
    timeout = int(raw)
    if not 1 <= timeout <= 120:
        raise ValueError(f"{COORDINATION_TIMEOUT_ENV} must be an integer from 1 to 120")
    return timeout


COORDINATION_TIMEOUT_SECONDS = parse_coordination_timeout(os.environ)


class WarmIdentityTests(unittest.TestCase):
    def test_directory_enumeration_failure_requests_cold_rebuild(self):
        spec = importlib.util.spec_from_file_location('warm_identity_test', ROOT / 'bin/codex-surface-warm.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            for error in (PermissionError('denied'), FileNotFoundError('raced removal')):
                with self.subTest(error=type(error).__name__), mock.patch.object(module.os, 'listdir', side_effect=error):
                    with self.assertRaises(SystemExit) as raised:
                        module.identity(directory)
                    self.assertEqual(raised.exception.code, 3)


def write_skill(root: Path, directory: str, name: str, body: str, *, implicit=True) -> Path:
    skill_dir = root / directory
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} fixture\n---\n\n{body}\n",
        encoding="utf-8",
    )
    agents = skill_dir / "agents"
    agents.mkdir(exist_ok=True)
    (agents / "openai.yaml").write_text(
        "policy:\n  allow_implicit_invocation: " + ("true\n" if implicit else "false\n"),
        encoding="utf-8",
    )
    return skill_dir / "SKILL.md"


def base_manifest() -> dict:
    return {
        "schema_version": 1,
        "repo": "fixture",
        "skills": {
            "source_precedence": [
                "codex-product",
                "repo-claude",
                "claude-plugin",
                "global-agents",
                "codex-only",
            ],
            "preserve_product_managed": True,
            "disabled_roots": [
                ".agents/skills",
                "${HOME}/.codex/superpowers/skills",
                "${CODEX_HOME}/plugins/cache/openai-curated-remote/superpowers",
            ],
            "duplicate_choices": {
                "project:*": "repo-claude",
                "skill-creator": "codex-product",
                "superpowers:*": "claude-plugin:superpowers@superpowers-marketplace",
            },
            "project": {
                "root": ".claude/skills",
                "implicit": ["alpha"],
                "explicit_only": ["project-explicit"],
                "unlisted": "disabled",
            },
            "commands": {
                "root": ".claude/commands",
                "explicit_only": [],
                "unlisted": "disabled",
            },
            "global_agents": {
                "root": "${HOME}/.agents/skills",
                "implicit": [],
                "explicit_only": ["agentation"],
                "unlisted": "disabled",
            },
            "claude_plugins": {
                "root": "${HOME}/.claude/plugins/cache",
                "unlisted_packages": "disabled",
                "packages": [
                    {
                        "id": "superpowers@superpowers-marketplace",
                        "implicit": ["brainstorming"],
                        "explicit_only": [],
                        "unlisted": "disabled",
                    },
                    {
                        "id": "watch@claude-video",
                        "implicit": [],
                        "explicit_only": ["watch"],
                        "unlisted": "disabled",
                    },
                ],
            },
            "codex_only": {
                "root": ".codex-only/repos/taste-skill/skills",
                "namespace": "taste-skill",
                "unlisted": "disabled",
                "profiles": {"default": [], "design": ["taste-skill:minimalist-ui"]},
            },
        },
        "mcp": {
            "definition_sources": [".mcp.json", "mcp.local.json", "codex-product"],
            "default_profile": "default",
            "mode": "exact",
            "required_in_all_profiles": ["harness-rag"],
            "profiles": {
                "default": {"enabled": ["context7", "harness-rag"]},
                "work": {"enabled": ["computer-use", "context7", "harness-rag", "jira"]},
            },
        },
    }


class RuntimeConfigMergeTests(unittest.TestCase):
    def test_merge_keeps_saved_folder_trust_without_duplicating_candidate_roots(self):
        spec = importlib.util.spec_from_file_location(
            "codex_surface_merge_test", ROOT / "bin" / "codex-surface.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        self.addCleanup(sys.modules.pop, spec.name, None)
        spec.loader.exec_module(module)
        candidate = 'model = "m"\n\n[projects."/harness"]\ntrust_level = "trusted"\n'
        live = candidate + '\n[projects."/other repo"]\ntrust_level = "untrusted"\n'
        merged = module.merge_runtime_config(candidate, live, {}, Path("/codex-home"))
        self.assertEqual(
            tomllib.loads(merged)["projects"],
            {"/harness": {"trust_level": "trusted"}, "/other repo": {"trust_level": "untrusted"}},
        )


class CoordinationTimeoutTests(unittest.TestCase):
    def test_coordination_timeout_defaults_to_30_seconds(self):
        self.assertEqual(parse_coordination_timeout({}), 30)

    def test_coordination_timeout_accepts_bounded_integer_override(self):
        for value in ("1", "37", "120"):
            with self.subTest(value=value):
                self.assertEqual(
                    parse_coordination_timeout(
                        {"HARNESS_TEST_COORDINATION_TIMEOUT_SECONDS": value}
                    ),
                    int(value),
                )

    def test_coordination_timeout_rejects_non_integer_or_out_of_range_override(self):
        for value in ("", "0", "121", "1.5", "nan", "inf"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    ValueError,
                    "HARNESS_TEST_COORDINATION_TIMEOUT_SECONDS",
                ):
                    parse_coordination_timeout(
                        {"HARNESS_TEST_COORDINATION_TIMEOUT_SECONDS": value}
                    )


class SurfaceFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="codex-surface-test."))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.home = self.tmp / "home"
        self.repo = self.tmp / "repo"
        self.codex_home = self.repo / ".harness" / "codex"
        self.home.mkdir()
        self.repo.mkdir()
        self.codex_home.mkdir(parents=True)

        self.alpha = write_skill(self.repo / ".claude" / "skills", "alpha-dir", "alpha", "canonical")
        self.project_explicit = write_skill(
            self.repo / ".claude" / "skills", "project-explicit", "project-explicit", "explicit"
        )
        self.mirror_alpha = write_skill(
            self.repo / ".agents" / "skills", "alpha", "alpha", "divergent mirror"
        )
        write_skill(self.repo / ".agents" / "skills", "unused-project", "unused-project", "unused")
        self.agentation = write_skill(self.tmp / "agent-store", "agentation", "agentation", "global")
        global_skills = self.home / ".agents" / "skills"
        global_skills.mkdir(parents=True)
        (global_skills / "agentation").symlink_to(self.agentation.parent, target_is_directory=True)
        write_skill(self.home / ".agents" / "skills", "unused-global", "unused-global", "unused")

        plugin_root = self.home / ".claude" / "plugins" / "cache"
        self.superpower = write_skill(
            plugin_root / "superpowers-marketplace" / "superpowers" / "6.1.1" / "skills",
            "brainstorming",
            "superpowers:brainstorming",
            "claude plugin",
        )
        write_skill(
            plugin_root / "superpowers-marketplace" / "superpowers" / "6.0.0" / "skills",
            "brainstorming",
            "superpowers:brainstorming",
            "old plugin",
        )
        self.watch = write_skill(
            plugin_root / "claude-video" / "watch" / "0.2.0" / "skills",
            "watch",
            "watch:watch",
            "watch plugin",
        )
        write_skill(
            plugin_root / "ykdojo" / "dx" / "1.0.0" / "skills", "gha", "dx:gha", "disallowed"
        )
        self.native_superpower = write_skill(
            self.home / ".codex" / "superpowers" / "skills",
            "brainstorming",
            "superpowers:brainstorming",
            "native divergent",
        )
        self.curated_superpower = write_skill(
            self.codex_home / "plugins" / "cache" / "openai-curated-remote" / "superpowers" / "1" / "skills",
            "brainstorming",
            "superpowers:brainstorming",
            "curated divergent",
        )
        self.taste = write_skill(
            self.repo / ".codex-only" / "repos" / "taste-skill" / "skills",
            "minimalist",
            "minimalist-ui",
            "taste",
        )

        system_root = self.home / ".codex" / "skills" / ".system"
        write_skill(system_root, "skill-creator", "skill-creator", "system")

        self.manifest = self.repo / "config" / "codex-surface.json"
        self.manifest.parent.mkdir()
        self.manifest.write_text(json.dumps(base_manifest(), indent=2) + "\n", encoding="utf-8")
        (self.repo / ".mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "context7": {"command": "context7"},
                        "harness-rag": {"command": "rag"},
                        "jira": {"command": "jira"},
                    }
                }
            ),
            encoding="utf-8",
        )

    def resolver_environment(self, **updates):
        env = dict(os.environ)
        for inherited in (
            "HARNESS_CODEX_MCP_PROFILE",
            "HARNESS_CODEX_SKILL_PROFILE",
            "HARNESS_CODEX_GLOBAL_MCP_ALLOWLIST",
            "HARNESS_CODEX_APPS_ALLOWLIST",
            "HARNESS_CODEX_CONTEXT",
        ):
            env.pop(inherited, None)
        env.update(updates)
        return env

    def run_resolver(self, *extra, expect=0, **env_updates):
        command = [
            sys.executable,
            str(RESOLVER),
            "resolve",
            "--manifest",
            str(self.manifest),
            "--repo-root",
            str(self.repo),
            "--codex-home",
            str(self.codex_home),
            "--home",
            str(self.home),
            *extra,
        ]
        result = subprocess.run(
            command,
            env=self.resolver_environment(**env_updates),
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, expect, result.stderr)
        return result

    def read_catalog(self):
        return json.loads((self.codex_home / "skill-catalog.json").read_text(encoding="utf-8"))


class ResolverTests(SurfaceFixture):
    def test_exact_resolution_explicit_policy_and_managed_pruning(self):
        (self.project_explicit.parent / "agents" / "openai.yaml").write_text(
            "interface:\n  display_name: Project explicit\n"
            "policy:\n  allow_implicit_invocation: true\n"
            "dependencies:\n  tools:\n    - type: mcp\n      value: fixture\n",
            encoding="utf-8",
        )
        skills_dir = self.codex_home / "skills"
        skills_dir.mkdir()
        unmanaged = skills_dir / "user-owned"
        unmanaged.mkdir()
        (unmanaged / "keep").write_text("keep", encoding="utf-8")
        stale_target = self.tmp / "stale"
        stale_target.mkdir()
        (skills_dir / "old-managed").symlink_to(stale_target, target_is_directory=True)
        (skills_dir / ".harness-managed").write_text("old-managed\n", encoding="utf-8")

        self.run_resolver()
        catalog = self.read_catalog()
        by_name = {entry["name"]: entry for entry in catalog["skills"]}
        self.assertEqual(
            set(by_name),
            {
                "agentation",
                "alpha",
                "project-explicit",
                "skill-creator",
                "superpowers:brainstorming",
                "watch:watch",
            },
        )
        self.assertEqual(Path(by_name["alpha"]["source_path"]), self.alpha.resolve())
        self.assertEqual(Path(by_name["superpowers:brainstorming"]["source_path"]), self.superpower.resolve())
        self.assertEqual(
            by_name["skill-creator"]["exposed_path"],
            by_name["skill-creator"]["source_path"],
        )
        self.assertFalse((skills_dir / "skill-creator").exists())
        self.assertEqual(by_name["agentation"]["invocation"], "explicit_only")
        self.assertTrue((unmanaged / "keep").is_file())
        self.assertFalse((skills_dir / "old-managed").exists())

        explicit_path = Path(by_name["project-explicit"]["exposed_path"])
        explicit_yaml = (explicit_path.parent / "agents" / "openai.yaml").read_text()
        self.assertIn("allow_implicit_invocation: false", explicit_yaml)
        self.assertIn("display_name: Project explicit", explicit_yaml)
        self.assertIn("value: fixture", explicit_yaml)
        self.assertTrue(explicit_path.is_file())
        self.assertFalse(explicit_path.is_symlink())

        config = (self.codex_home / "surface.config.toml").read_text(encoding="utf-8")
        for disabled in (self.mirror_alpha, self.native_superpower, self.curated_superpower):
            self.assertIn(f'path = {json.dumps(str(disabled.resolve()))}', config)
        self.assertNotIn("${HOME}", json.dumps(catalog))
        self.assertNotIn("${CODEX_HOME}", json.dumps(catalog))
        self.assertFalse(any("dx:gha" == entry["name"] for entry in catalog["skills"]))

        before = (self.codex_home / "skill-catalog.json").stat().st_mtime_ns
        self.run_resolver()
        self.assertEqual(before, (self.codex_home / "skill-catalog.json").stat().st_mtime_ns)

    def test_design_profile_is_exact_and_missing_source_fails(self):
        self.run_resolver("--skill-profile", "design")
        names = {entry["name"] for entry in self.read_catalog()["skills"]}
        self.assertIn("taste-skill:minimalist-ui", names)
        self.taste.unlink()
        result = self.run_resolver("--skill-profile", "design", expect=2)
        self.assertIn("taste-skill:minimalist-ui", result.stderr)

    def test_divergent_duplicate_requires_manifest_choice(self):
        manifest = base_manifest()
        del manifest["skills"]["duplicate_choices"]["project:*"]
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        result = self.run_resolver(expect=2)
        self.assertIn("divergent duplicate", result.stderr)
        self.assertIn("alpha", result.stderr)

    def test_identical_selected_duplicates_follow_source_precedence(self):
        self.mirror_alpha.write_bytes(self.alpha.read_bytes())
        project = write_skill(
            self.repo / ".claude" / "skills", "identical", "identical", "same bytes"
        )
        global_target = write_skill(self.tmp / "identical-store", "identical", "identical", "same bytes")
        global_target.write_bytes(project.read_bytes())
        (self.home / ".agents" / "skills" / "identical").symlink_to(
            global_target.parent, target_is_directory=True
        )
        manifest = base_manifest()
        del manifest["skills"]["duplicate_choices"]["project:*"]
        manifest["skills"]["project"]["implicit"].append("identical")
        manifest["skills"]["global_agents"]["implicit"].append("identical")
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        self.run_resolver()
        entry = next(item for item in self.read_catalog()["skills"] if item["name"] == "identical")
        self.assertEqual(entry["source"], "repo-claude")
        self.assertEqual(Path(entry["source_path"]), project.resolve())

    def test_exact_mcp_profiles_validate_required_servers(self):
        self.run_resolver("--mcp-profile", "default")
        catalog = self.read_catalog()
        self.assertEqual(catalog["mcp"]["enabled"], ["context7", "harness-rag"])
        self.assertEqual(catalog["mcp"]["disabled"], ["jira"])
        self.run_resolver("--mcp-profile", "work")
        catalog = self.read_catalog()
        self.assertEqual(catalog["mcp"]["enabled"], ["computer-use", "context7", "harness-rag", "jira"])
        self.assertEqual(catalog["mcp"]["product_managed"], ["computer-use"])

        manifest = base_manifest()
        manifest["mcp"]["profiles"]["work"]["enabled"].remove("harness-rag")
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        result = self.run_resolver("--mcp-profile", "work", expect=2)
        self.assertIn("required_in_all_profiles", result.stderr)

    def test_global_allowlist_duplicate_with_product_source_fails(self):
        # Removing codex-product duplicate detection would silently let an
        # allowlisted global definition override the product-managed server.
        manifest = base_manifest()
        manifest["mcp"]["definition_sources"].append("codex-global-allowlist")
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        global_config = self.home / ".codex" / "config.toml"
        global_config.parent.mkdir(exist_ok=True)
        global_config.write_text(
            '[mcp_servers.computer-use]\ncommand = "global-computer-use"\n',
            encoding="utf-8",
        )

        result = self.run_resolver(
            expect=2,
            HARNESS_CODEX_GLOBAL_MCP_ALLOWLIST="computer-use",
        )

        self.assertIn("computer-use", result.stderr)
        self.assertIn("codex-product", result.stderr)

    def test_global_allowlist_duplicate_with_each_json_source_fails(self):
        for source_name in (".mcp.json", ".mcp.local.json", "mcp.local.json"):
            with self.subTest(source_name=source_name):
                server = {
                    ".mcp.json": "context7",
                    ".mcp.local.json": "local-dot",
                    "mcp.local.json": "local-bare",
                }[source_name]
                for local_name in (".mcp.local.json", "mcp.local.json"):
                    (self.repo / local_name).unlink(missing_ok=True)
                if source_name != ".mcp.json":
                    (self.repo / source_name).write_text(
                        json.dumps({"mcpServers": {server: {"command": "duplicate"}}}),
                        encoding="utf-8",
                    )
                manifest = base_manifest()
                manifest["mcp"]["definition_sources"].append("codex-global-allowlist")
                self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
                global_config = self.home / ".codex" / "config.toml"
                global_config.parent.mkdir(exist_ok=True)
                global_config.write_text(
                    f'[mcp_servers.{server}]\ncommand = "global-{server}"\n',
                    encoding="utf-8",
                )

                result = self.run_resolver(
                    expect=2,
                    HARNESS_CODEX_GLOBAL_MCP_ALLOWLIST=server,
                )

                self.assertIn(server, result.stderr)
                self.assertIn("codex-global-allowlist", result.stderr)

    def test_mcp_profile_policies_reject_invalid_shapes_and_membership(self):
        cases = {
            "non-object": [],
            "unknown-field": {"context7": {"tools": ["read"]}},
            "duplicate-tool": {"context7": {"enabled_tools": ["read", "read"]}},
            "empty-tool": {"context7": {"disabled_tools": [""]}},
            "overlap": {"context7": {"enabled_tools": ["read"], "disabled_tools": ["read"]}},
            "disabled-member": {"jira": {"enabled_tools": ["read"]}},
            "null-timeout": {"context7": {"startup_timeout_sec": None}},
            "nan-timeout": {"context7": {"tool_timeout_sec": float("nan")}},
            "infinite-timeout": {"context7": {"tool_timeout_sec": float("inf")}},
            "null-approval": {"context7": {"default_tools_approval_mode": None}},
            "empty-tools-policy": {"context7": {"tools": {}}},
        }
        for label, policies in cases.items():
            with self.subTest(label=label):
                manifest = base_manifest()
                manifest["mcp"]["profiles"]["default"]["policies"] = policies
                self.manifest.write_text(json.dumps(manifest), encoding="utf-8")

                result = self.run_resolver(expect=2)

                self.assertIn("policies", result.stderr)

    def test_mcp_profile_runtime_policy_is_preserved_in_catalog(self):
        manifest = base_manifest()
        manifest["mcp"]["profiles"]["default"]["policies"] = {
            "context7": {
                "startup_timeout_sec": 10,
                "tool_timeout_sec": 45,
                "required": True,
                "default_tools_approval_mode": "approve",
                "tools": {
                    "resolve-library-id": {"approval_mode": "approve"},
                },
            }
        }
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")

        self.run_resolver()
        catalog = self.read_catalog()
        expected = manifest["mcp"]["profiles"]["default"]["policies"]["context7"]
        self.assertEqual(catalog["mcp"]["policies"]["context7"], expected)

    def test_explicit_optional_policy_is_dropped_when_host_definition_is_absent(self):
        manifest = base_manifest()
        manifest['mcp']['profiles']['default']['enabled'].append('host-service')
        manifest['mcp']['profiles']['default']['policies'] = {
            'host-service': {'required': False, 'tools': {'read': {'approval_mode': 'approve'}}}
        }
        self.manifest.write_text(json.dumps(manifest), encoding='utf-8')
        self.run_resolver()
        catalog = self.read_catalog()['mcp']
        self.assertNotIn('host-service', catalog['enabled'])
        self.assertNotIn('host-service', catalog['policies'])
        manifest['mcp']['profiles']['default']['policies']['host-service']['required'] = True
        self.manifest.write_text(json.dumps(manifest), encoding='utf-8')
        rejected = self.run_resolver(expect=2)
        self.assertIn('has no emitted MCP definition', rejected.stderr)

    def test_product_managed_mcp_policy_without_emitted_definition_fails(self):
        # Product/plugin-only computer-use has no generated [mcp_servers] table
        # where a tool policy could be applied.
        manifest = base_manifest()
        manifest["mcp"]["profiles"]["default"]["enabled"].append("computer-use")
        manifest["mcp"]["profiles"]["default"]["policies"] = {
            "computer-use": {"enabled_tools": ["computer-use"]}
        }
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")

        result = self.run_resolver(expect=2)

        self.assertIn("computer-use", result.stderr)
        self.assertIn("emitted MCP definition", result.stderr)

    def test_enabled_without_definition_is_dropped_not_fatal(self):
        # A server enabled in a profile but absent from every definition source
        # (e.g. a host-local MCP not present on this machine) is dropped, not fatal.
        # mcp.local.json (gitignored) thereby decides per-host exposure. A stderr
        # note keeps the drop visible so a typo does not vanish silently.
        manifest = base_manifest()
        manifest["mcp"]["profiles"]["default"]["enabled"].append("host-local-rag")
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        result = self.run_resolver("--mcp-profile", "default")
        catalog = self.read_catalog()
        self.assertEqual(catalog["mcp"]["enabled"], ["context7", "harness-rag"])
        self.assertNotIn("host-local-rag", catalog["mcp"]["enabled"])
        self.assertIn("host-local-rag", result.stderr)


@unittest.skipUnless(Path("/usr/bin/lockf").is_file(), "requires macOS /usr/bin/lockf")
class PrepareIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="codex-surface-prepare."))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.home = self.tmp / "home"
        self.repo = self.tmp / "repo"
        self.codex_home = self.repo / ".harness" / "codex"
        self.home.mkdir()
        self.repo.mkdir()
        self.counter = self.tmp / "compiler-calls"
        self.no_marketplace = self.tmp / "no-marketplace"
        self.codex_bin = self.tmp / "codex"
        self.codex_bin.write_text(
            '#!/usr/bin/env bash\necho "codex-cli 0.153.2"\n',
            encoding="utf-8",
        )
        self.codex_bin.chmod(0o755)

        computer_plugin = self.no_marketplace / "plugins" / "computer-use"
        (computer_plugin / ".codex-plugin").mkdir(parents=True)
        (computer_plugin / ".codex-plugin" / "plugin.json").write_text(
            json.dumps({"name": "computer-use", "version": "1.0.0"}) + "\n",
            encoding="utf-8",
        )
        write_skill(
            computer_plugin / "skills",
            "computer-use",
            "computer-use",
            "bundled computer use workflow",
        )
        chrome_plugin = self.no_marketplace / "plugins" / "chrome" / ".codex-plugin"
        chrome_plugin.mkdir(parents=True)
        (chrome_plugin / "plugin.json").write_text(
            json.dumps({"name": "chrome", "version": "1.0.0"}) + "\n",
            encoding="utf-8",
        )

        write_skill(self.repo / ".claude" / "skills", "alpha", "alpha", "alpha v1")
        write_skill(
            self.repo / ".claude" / "skills", "explicit", "project-explicit", "explicit v1"
        )
        write_skill(self.repo / ".agents" / "skills", "unused-mirror", "unused-mirror", "disabled")
        plugin = write_skill(
            self.home
            / ".claude"
            / "plugins"
            / "cache"
            / "superpowers-marketplace"
            / "superpowers"
            / "6.1.1"
            / "skills",
            "brainstorming",
            "superpowers:brainstorming",
            "plugin v1",
        )
        self.plugin_skill = plugin

        manifest = base_manifest()
        manifest["skills"]["project"]["implicit"] = ["alpha"]
        manifest["skills"]["project"]["explicit_only"] = ["project-explicit"]
        manifest["skills"]["global_agents"]["explicit_only"] = []
        manifest["skills"]["claude_plugins"]["packages"] = [
            {
                "id": "superpowers@superpowers-marketplace",
                "implicit": ["brainstorming"],
                "explicit_only": [],
                "unlisted": "disabled",
            }
        ]
        manifest["skills"]["codex_only"]["profiles"] = {"default": []}
        manifest["mcp"]["profiles"] = {
            "default": {"enabled": ["context7", "harness-rag"]},
            "work": {"enabled": ["computer-use", "context7", "harness-rag", "jira"]},
        }
        (self.repo / "config").mkdir()
        self.manifest_path = self.repo / "config" / "codex-surface.json"
        self.manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        self.mcp_path = self.repo / ".mcp.json"
        self.mcp_path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "context7": {"command": "context7"},
                        "harness-rag": {"command": "rag"},
                        "jira": {"command": "jira"},
                    }
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        source = self.repo / ".claude" / "source"
        source.mkdir(parents=True)
        (source / "runtime-contract.yaml").write_text("version: 1\n", encoding="utf-8")
        compiler = self.repo / "core" / "scripts" / "harness_compile.py"
        compiler.parent.mkdir(parents=True)
        compiler.write_text(
            """import os
from pathlib import Path
import sys

repo = Path(sys.argv[-1])
counter = Path(os.environ["HARNESS_TEST_COMPILER_COUNTER"])
with counter.open("a", encoding="utf-8") as stream:
    stream.write("call\\n")
if os.environ.get("HARNESS_TEST_COMPILER_FAIL") == "1":
    raise SystemExit(23)
out = repo / ".harness" / "codex"
out.mkdir(parents=True, exist_ok=True)
(out / "AGENTS.md").write_text("# generated fixture\\n", encoding="utf-8")
""",
            encoding="utf-8",
        )
        (self.repo / "CLAUDE.md").write_text("# fixture\n", encoding="utf-8")
        hooks = self.repo / "core" / "hooks"
        hooks.mkdir(parents=True)
        (hooks / "session-start.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (self.repo / ".claude" / "settings.json").write_text("{\"hooks\": {}}\n", encoding="utf-8")

    def environment(self, **updates):
        env = dict(os.environ)
        for inherited in (
            "HARNESS_CODEX_CONTEXT",
            "HARNESS_CODEX_MCP_PROFILE",
            "HARNESS_CODEX_SKILL_PROFILE",
            "HARNESS_CODEX_GLOBAL_MCP_ALLOWLIST",
            "HARNESS_CODEX_APPS_ALLOWLIST",
            "HARNESS_ORCA_AGENT_HOOKS",
            "HARNESS_HERDR_AGENT_HOOKS",
            "HARNESS_LAUNCH_RECORD_HOOKS",
        ):
            env.pop(inherited, None)
        env.update(
            {
                "HOME": str(self.home),
                "HARNESS_CODEX_BIN": str(self.codex_bin),
                "HARNESS_CODEX_BUNDLED_MARKETPLACE_SOURCE": str(self.no_marketplace),
                "HARNESS_TEST_COMPILER_COUNTER": str(self.counter),
            }
        )
        env.update(updates)
        return env

    def prepare(self, *, expect=0, **env_updates):
        result = subprocess.run(
            ["/bin/bash", str(PREPARE), str(self.repo)],
            env=self.environment(**env_updates),
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, expect, result.stderr)
        return result

    def test_default_fixture_context_is_independent_of_parent_but_explicit_context_is_honored(self):
        with mock.patch.dict(os.environ, {"HARNESS_CODEX_CONTEXT": "1m"}):
            self.prepare()
            with (self.codex_home / "config.toml").open("rb") as stream:
                self.assertNotIn("model_context_window", tomllib.load(stream))
            self.prepare(HARNESS_CODEX_CONTEXT="1m")
            with (self.codex_home / "config.toml").open("rb") as stream:
                self.assertEqual(tomllib.load(stream)["model_context_window"], 1000000)

    def test_explicit_500k_output(self):
        self.prepare(HARNESS_CODEX_CONTEXT="500k")
        with (self.codex_home / "config.toml").open("rb") as stream:
            data = tomllib.load(stream)
        self.assertEqual((data["model_context_window"], data["model_auto_compact_token_limit"]), (500000, 400000))

    def test_context_modes_and_single_stale_key_are_repaired(self):
        config_path = self.codex_home / "config.toml"
        values = {"272k": (272000, 217600), "500k": (500000, 400000), "1m": (1000000, 414000)}
        for mode in ("auto", "272k", "500k", "1m", "auto", "500k", "272k", "auto"):
            self.prepare(HARNESS_CODEX_CONTEXT=mode)
            with config_path.open("rb") as stream:
                data = tomllib.load(stream)
            if mode == "auto":
                self.assertNotIn("model_context_window", data)
                self.assertNotIn("model_auto_compact_token_limit", data)
            else:
                self.assertEqual((data["model_context_window"], data["model_auto_compact_token_limit"]), values[mode])
        for key in ("model_context_window", "model_auto_compact_token_limit"):
            config = config_path.read_text()
            config_path.write_text(config.replace('model_reasoning_effort = "medium"', 'model_reasoning_effort = "medium"\n' + key + ' = 123'))
            self.prepare(HARNESS_CODEX_CONTEXT="auto")
            with config_path.open("rb") as stream:
                self.assertNotIn(key, tomllib.load(stream))

    def test_homebrew_compat_and_opt_entrypoints_generate_identical_hook_paths(self):
        prefix = self.tmp / "brew"
        keg = prefix / "Cellar" / "harness-launcher" / "0.24.0"
        package = keg / "share" / "harness-launcher"
        shutil.copytree(ROOT / "bin", package)
        (prefix / "opt").mkdir(parents=True)
        (prefix / "opt" / "harness-launcher").symlink_to(keg, target_is_directory=True)
        (prefix / "share").mkdir()
        compat = prefix / "share" / "harness-launcher"
        compat.symlink_to(package, target_is_directory=True)
        stable = prefix / "opt" / "harness-launcher" / "share" / "harness-launcher"
        settings = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "bash " + str(self.repo / "core/hooks/session-start.sh")}]}]}}
        (self.repo / ".claude/settings.json").write_text(json.dumps(settings))
        rendered = []
        # A shell route that resolves symlinks enters through the versioned keg.
        for entry in (compat, stable, package, compat):
            (self.codex_home / ".surface-success.json").unlink(missing_ok=True)
            result = subprocess.run(["/bin/bash", str(entry / "codex-home-prepare.sh"), str(self.repo)], env=self.environment(), capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            rendered.append((self.codex_home / "hooks.json").read_bytes())
        self.assertEqual(rendered[0], rendered[1])
        self.assertEqual(rendered[1], rendered[2])
        self.assertEqual(rendered[2], rendered[3])
        self.assertIn(str(stable).encode(), rendered[0])
        self.assertNotIn(b"/Cellar/", rendered[2])
        # A different active keg is not an equivalent alias and must not win.
        other_keg = prefix / "Cellar" / "harness-launcher" / "other"
        shutil.copytree(package, other_keg / "share/harness-launcher")
        (prefix / "opt/harness-launcher").unlink()
        (prefix / "opt/harness-launcher").symlink_to(other_keg, target_is_directory=True)
        (self.codex_home / ".surface-success.json").unlink(missing_ok=True)
        result = subprocess.run(["/bin/bash", str(compat / "codex-home-prepare.sh"), str(self.repo)], env=self.environment(), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(str(compat).encode(), (self.codex_home / "hooks.json").read_bytes())
        self.assertNotIn(str(stable).encode(), (self.codex_home / "hooks.json").read_bytes())

    def compiler_calls(self):
        if not self.counter.exists():
            return 0
        return len(self.counter.read_text(encoding="utf-8").splitlines())

    def managed_output_paths(self):
        result = subprocess.run(
            [
                sys.executable,
                str(RESOLVER),
                "managed-output-paths",
                "--codex-home",
                str(self.codex_home),
            ],
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def snapshot_paths(self, relative_paths):
        snapshot = {}

        def capture(path, relative):
            if path.is_symlink():
                snapshot[relative] = ["symlink", os.readlink(path)]
            elif path.is_file():
                snapshot[relative] = ["file", hashlib.sha256(path.read_bytes()).hexdigest()]
            elif path.is_dir():
                snapshot[relative] = ["directory"]
                for child in sorted(path.iterdir(), key=lambda item: item.name):
                    capture(child, f"{relative}/{child.name}")
            else:
                snapshot[relative] = ["missing"]

        for relative in relative_paths:
            capture(self.codex_home / relative, relative)
        return snapshot

    def staging_artifacts(self):
        return sorted(self.codex_home.parent.glob(".codex-home-prepare-stage.*"))

    def snapshot_tree(self, root):
        snapshot = {}

        def capture(path, relative):
            if path.is_symlink():
                snapshot[relative] = ["symlink", os.readlink(path)]
            elif path.is_file():
                snapshot[relative] = ["file", hashlib.sha256(path.read_bytes()).hexdigest()]
            elif path.is_dir():
                snapshot[relative] = ["directory"]
                for child in sorted(path.iterdir(), key=lambda item: item.name):
                    capture(child, f"{relative}/{child.name}")
            else:
                snapshot[relative] = ["missing"]

        capture(root, ".")
        return snapshot

    def start_prepare(self, *, start_new_session=False, **env_updates):
        return subprocess.Popen(
            ["/bin/bash", str(PREPARE), str(self.repo)],
            env=self.environment(**env_updates),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=start_new_session,
        )

    def wait_for_condition(self, process, predicate, label, timeout=None):
        if timeout is None:
            timeout = COORDINATION_TIMEOUT_SECONDS
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                self.fail(
                    f"prepare exited before {label}\n"
                    f"returncode={process.returncode}\nstdout={stdout}\nstderr={stderr}"
                )
            time.sleep(0.01)
        process.kill()
        stdout, stderr = process.communicate()
        self.fail(f"prepare did not reach {label}\nstdout={stdout}\nstderr={stderr}")

    def enabled_value(self, server):
        config = (self.codex_home / "config.toml").read_text(encoding="utf-8")
        match = re.search(
            rf"(?ms)^\[mcp_servers\.{re.escape(server)}\]\n(.*?)(?=^\[|\Z)", config
        )
        self.assertIsNotNone(match, f"missing MCP table for {server}")
        enabled = re.search(r"^enabled = (true|false)$", match.group(1), re.MULTILINE)
        self.assertIsNotNone(enabled, f"missing enabled flag for {server}")
        return enabled.group(1) == "true"

    def plugin_enabled(self, plugin):
        config = (self.codex_home / "config.toml").read_text(encoding="utf-8")
        match = re.search(
            rf'(?ms)^\[plugins\."{re.escape(plugin)}@openai-bundled"\]\n(.*?)(?=^\[|\Z)',
            config,
        )
        self.assertIsNotNone(match, f"missing plugin table for {plugin}")
        return "enabled = true" in match.group(1)

    def test_missing_default_app_marketplace_uses_valid_cached_marketplace(self):
        cached = (
            self.home
            / ".codex"
            / ".tmp"
            / "bundled-marketplaces"
            / "openai-bundled"
        )
        cached.parent.mkdir(parents=True)
        shutil.copytree(self.no_marketplace, cached)
        env = self.environment(HARNESS_CODEX_MCP_PROFILE="work")
        env.pop("HARNESS_CODEX_BUNDLED_MARKETPLACE_SOURCE", None)
        app_marketplace = self.tmp / "missing-codex-app-marketplace"
        env["HARNESS_CODEX_APP_BUNDLED_MARKETPLACE_SOURCE"] = str(app_marketplace)

        result = subprocess.run(
            ["/bin/bash", str(PREPARE), str(self.repo)],
            env=env,
            text=True,
            capture_output=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(
            (
                self.codex_home
                / "plugins"
                / "cache"
                / "openai-bundled"
                / "computer-use"
                / "latest"
                / ".codex-plugin"
                / "plugin.json"
            ).is_file()
        )

        shutil.copytree(self.no_marketplace, app_marketplace)
        app_manifest = (
            app_marketplace
            / "plugins"
            / "computer-use"
            / ".codex-plugin"
            / "plugin.json"
        )
        app_manifest.write_text(
            json.dumps({"name": "computer-use", "version": "2.0.0"}) + "\n",
            encoding="utf-8",
        )
        second = subprocess.run(
            ["/bin/bash", str(PREPARE), str(self.repo)],
            env=env,
            text=True,
            capture_output=True,
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(
            os.readlink(
                self.codex_home
                / "plugins"
                / "cache"
                / "openai-bundled"
                / "computer-use"
                / "latest"
            ),
            "2.0.0",
        )

    def test_explicit_marketplace_source_switch_invalidates_warm_home(self):
        source_a = self.tmp / "marketplace-a"
        source_b = self.tmp / "marketplace-b"
        shutil.copytree(self.no_marketplace, source_a)
        shutil.copytree(self.no_marketplace, source_b)
        manifest_b = source_b / "plugins/computer-use/.codex-plugin/plugin.json"
        manifest_b.write_text(
            json.dumps({"name": "computer-use", "version": "3.0.0"}) + "\n",
            encoding="utf-8",
        )

        self.prepare(
            HARNESS_CODEX_MCP_PROFILE="work",
            HARNESS_CODEX_BUNDLED_MARKETPLACE_SOURCE=str(source_a),
        )
        self.prepare(
            HARNESS_CODEX_MCP_PROFILE="work",
            HARNESS_CODEX_BUNDLED_MARKETPLACE_SOURCE=str(source_b),
        )

        self.assertEqual(
            os.readlink(
                self.codex_home
                / "plugins"
                / "cache"
                / "openai-bundled"
                / "computer-use"
                / "latest"
            ),
            "3.0.0",
        )

    def test_external_manifest_can_opt_in_computer_use_and_stay_warm(self):
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        manifest["mcp"]["profiles"]["default"]["enabled"].append("computer-use")
        self.manifest_path.write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )

        self.prepare()

        catalog = json.loads(
            (self.codex_home / "skill-catalog.json").read_text(encoding="utf-8")
        )
        self.assertIn("computer-use", catalog["mcp"]["enabled"])
        self.assertIn("computer-use", catalog["mcp"]["product_managed"])
        self.assertIn(
            "computer-use:computer-use",
            {item["name"] for item in catalog["skills"]},
        )
        self.assertTrue(self.plugin_enabled("computer-use"))
        self.assertTrue(
            (
                self.codex_home
                / "plugins"
                / "cache"
                / "openai-bundled"
                / "computer-use"
                / "latest"
                / ".codex-plugin"
                / "plugin.json"
            ).is_file()
        )
        self.assertEqual(self.compiler_calls(), 1)

        self.prepare()

        self.assertEqual(
            self.compiler_calls(),
            1,
            "external computer-use opt-in did not stay warm",
        )

    def test_runtime_in_use_marker_changes_stay_warm(self):
        self.prepare()
        self.assertEqual(self.compiler_calls(), 1)
        marker = self.plugin_skill.parents[2] / ".in_use"
        marker.write_text("first\n", encoding="utf-8")
        self.prepare()
        self.assertEqual(self.compiler_calls(), 1, "creating .in_use regenerated the surface")
        marker.write_text("second\n", encoding="utf-8")
        self.prepare()
        self.assertEqual(self.compiler_calls(), 1, "changing .in_use regenerated the surface")
        marker.unlink()
        self.prepare()
        self.assertEqual(self.compiler_calls(), 1, "removing .in_use regenerated the surface")

    def test_folder_trust_entries_stay_warm(self):
        # config.toml carries [projects] trust for the launcher's root and any
        # decision Codex saved; neither may turn the warm check cold.
        self.prepare()
        config_path = self.codex_home / "config.toml"
        root = str(self.repo.resolve())
        with open(config_path, "rb") as stream:
            self.assertEqual(tomllib.load(stream)["projects"], {root: {"trust_level": "trusted"}})
        with open(config_path, "a", encoding="utf-8") as stream:
            stream.write('\n[projects."/tmp/other repo"]\ntrust_level = "untrusted"\n')
        self.prepare()
        self.prepare()
        self.assertEqual(self.compiler_calls(), 1, "folder trust entries regenerated the surface")
        with open(config_path, "rb") as stream:
            self.assertEqual(tomllib.load(stream)["projects"]["/tmp/other repo"], {"trust_level": "untrusted"})

    def test_in_use_nonregular_and_entry_type_transitions_invalidate(self):
        self.prepare()
        root = self.plugin_skill.parents[2]
        marker = root / ".in_use"
        marker.mkdir()
        (marker / "SKILL.md").write_text("---\nname: marker\n---\n", encoding="utf-8")
        self.prepare()
        self.assertEqual(self.compiler_calls(), 2, "directory .in_use stayed warm")
        shutil.rmtree(marker)
        marker.symlink_to(root / "missing-target")
        self.prepare()
        self.assertEqual(self.compiler_calls(), 3, "symlink .in_use stayed warm")
        marker.unlink()
        same = root / "type-transition"
        same.write_text("plain\n", encoding="utf-8")
        self.prepare()
        calls = self.compiler_calls()
        same.unlink(); same.mkdir(); (same / "SKILL.md").write_text("---\nname: type\n---\n", encoding="utf-8")
        self.prepare()
        self.assertEqual(self.compiler_calls(), calls + 1, "file-to-skilldir stayed warm")
        shutil.rmtree(same); same.symlink_to(root / "missing")
        self.prepare(); calls = self.compiler_calls()
        same.unlink(); same.mkdir(); (same / "SKILL.md").write_text("---\nname: repaired\n---\n", encoding="utf-8")
        self.prepare()
        self.assertEqual(self.compiler_calls(), calls + 1, "brokenlink-to-skilldir stayed warm")
        shutil.rmtree(same); same.write_text("plain again\n", encoding="utf-8")
        self.prepare()
        self.assertEqual(self.compiler_calls(), calls + 2, "skilldir-to-file stayed warm")

    def orca_command(self):
        return (
            "/bin/sh -c 's=\"$HOME/.orca/agent-hooks/codex-hook.sh\"; "
            "[ -x \"$s\" ] && { /bin/sh \"$s\" >/dev/null 2>&1; exit 0; }; cat >/dev/null'"
        )

    def set_launcher_env(self, *lines):
        path = self.repo / "config" / "launcher.env"
        path.parent.mkdir(exist_ok=True)
        path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")

    def test_orca_agent_hooks_opt_in_comes_only_from_launcher_env(self):
        events = ["SessionStart", "UserPromptSubmit", "PreToolUse",
                  "PermissionRequest", "PostToolUse", "Stop"]
        hooks_path = self.codex_home / "hooks.json"
        self.set_launcher_env('HARNESS_PREFIX="x"')
        self.prepare()
        self.assertEqual(self.compiler_calls(), 1)
        baseline = hooks_path.read_bytes()
        self.assertNotIn(b"agent-hooks", baseline)

        # Process env alone never enables it, and never invalidates the home.
        self.prepare(HARNESS_ORCA_AGENT_HOOKS="1")
        self.assertEqual(self.compiler_calls(), 1, "ambient env changed the fingerprint")
        self.assertEqual(hooks_path.read_bytes(), baseline)

        # launcher.env opt-in with an empty environment (direct prepare call).
        self.set_launcher_env('HARNESS_PREFIX="x"', "export HARNESS_ORCA_AGENT_HOOKS='1'")
        self.prepare()
        self.assertEqual(self.compiler_calls(), 2, "opt-in did not invalidate the warm home")
        hooks = json.loads(hooks_path.read_text(encoding="utf-8"))["hooks"]
        base_hooks = json.loads(baseline)["hooks"]
        for event in events:
            entry = {"hooks": [{"type": "command", "command": self.orca_command(), "timeout": 5}]}
            self.assertEqual(hooks[event][-1], entry, event)
            self.assertNotIn("matcher", hooks[event][-1])
            self.assertEqual(hooks[event][:-1], base_hooks.get(event, []), event)
        enabled = hooks_path.read_bytes()

        # Launcher-style (env exported) and direct calls agree: no ping-pong.
        for env in ({}, {"HARNESS_ORCA_AGENT_HOOKS": "1"}, {"HARNESS_ORCA_AGENT_HOOKS": ""}, {}):
            self.prepare(**env)
            self.assertEqual(self.compiler_calls(), 2, f"cold rebuild with env {env}")
            self.assertEqual(hooks_path.read_bytes(), enabled)

        # Value forms: last assignment wins; anything but 1 is off.
        self.set_launcher_env("HARNESS_ORCA_AGENT_HOOKS=1", 'HARNESS_ORCA_AGENT_HOOKS="0"')
        self.prepare()
        self.assertEqual(self.compiler_calls(), 3, "opt-out did not invalidate the warm home")
        self.assertEqual(hooks_path.read_bytes(), baseline)
        self.set_launcher_env('HARNESS_ORCA_AGENT_HOOKS="1"')
        self.prepare()
        self.assertEqual(self.compiler_calls(), 4)
        self.assertEqual(hooks_path.read_bytes(), enabled)
        (self.repo / "config" / "launcher.env").unlink()
        self.prepare()
        self.assertEqual(self.compiler_calls(), 5)
        self.assertEqual(hooks_path.read_bytes(), baseline)

    def test_orca_hook_wrapper_is_status_only_and_fail_open(self):
        self.set_launcher_env("HARNESS_ORCA_AGENT_HOOKS=1")
        self.prepare()
        hooks = json.loads((self.codex_home / "hooks.json").read_text(encoding="utf-8"))["hooks"]
        command = hooks["PreToolUse"][-1]["hooks"][0]["command"]
        script_dir = self.home / ".orca" / "agent-hooks"
        script_dir.mkdir(parents=True)
        script = script_dir / "codex-hook.sh"
        script.write_text('#!/bin/sh\ncat > "$HOME/stdin.seen"\necho \'{"decision":"block"}\'\nexit 2\n', encoding="utf-8")
        script.chmod(0o755)
        result = subprocess.run(command, shell=True, input="payload\n", text=True,
                                capture_output=True, env={"HOME": str(self.home), "PATH": "/usr/bin:/bin"})
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual((self.home / "stdin.seen").read_text(encoding="utf-8"), "payload\n")
        script.unlink()
        result = subprocess.run(command, shell=True, input="payload\n", text=True,
                                capture_output=True, env={"HOME": str(self.home), "PATH": "/usr/bin:/bin"})
        self.assertEqual((result.returncode, result.stdout), (0, ""))

    HERDR_COMMAND = (
        "/bin/sh -c 's=\"$HOME/.codex/herdr-agent-state.sh\"; "
        "[ -x \"$s\" ] && { /bin/sh \"$s\" session >/dev/null 2>&1; exit 0; }; cat >/dev/null'"
    )

    def herdr_entry(self):
        return {"hooks": [{"type": "command", "command": self.HERDR_COMMAND, "timeout": 10}]}

    def install_herdr_script(self, body='#!/bin/sh\nexit 0\n'):
        script = self.home / ".codex" / "herdr-agent-state.sh"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(body, encoding="utf-8")
        script.chmod(0o755)
        return script

    def read_hooks(self):
        return json.loads((self.codex_home / "hooks.json").read_text(encoding="utf-8"))["hooks"]

    def test_herdr_opt_in_comes_only_from_launcher_env(self):
        # Case 1 (no opt-in, script present) and case 7 (process env only).
        self.install_herdr_script()
        hooks_path = self.codex_home / "hooks.json"
        self.set_launcher_env('HARNESS_PREFIX="x"')
        self.prepare()
        self.assertEqual(self.compiler_calls(), 1)
        baseline = hooks_path.read_bytes()
        self.assertNotIn(b"herdr-agent-state.sh", baseline)
        self.assertNotIn(self.herdr_entry(), self.read_hooks().get("SessionStart", []))
        self.assertNotIn(b"agent-hooks", baseline)
        self.prepare(HARNESS_HERDR_AGENT_HOOKS="1")
        self.assertEqual(self.compiler_calls(), 1, "ambient env changed the fingerprint")
        self.assertEqual(hooks_path.read_bytes(), baseline)
        # An explicit off value stays off and does not change the fingerprint.
        self.set_launcher_env("HARNESS_HERDR_AGENT_HOOKS=0")
        self.prepare()
        self.assertEqual(self.compiler_calls(), 1, "an off value must not change the fingerprint")
        self.assertEqual(hooks_path.read_bytes(), baseline)
        # A missing launcher.env resolves off the same way at prepare level.
        (self.repo / "config" / "launcher.env").unlink()
        self.prepare()
        self.assertEqual(self.compiler_calls(), 1, "a missing launcher.env must resolve off")
        self.assertEqual(hooks_path.read_bytes(), baseline)

    def test_herdr_opt_in_appends_one_fail_open_session_start_entry(self):
        # Cases 2 and 3: identical output with and without the script.
        hooks_path = self.codex_home / "hooks.json"
        self.set_launcher_env('HARNESS_PREFIX="x"')
        self.prepare()
        baseline = hooks_path.read_bytes()
        base_hooks = json.loads(baseline)["hooks"]

        self.set_launcher_env('HARNESS_PREFIX="x"', "HARNESS_HERDR_AGENT_HOOKS=1")
        self.assertFalse((self.home / ".codex" / "herdr-agent-state.sh").exists())
        self.prepare()
        absent = hooks_path.read_bytes()
        hooks = self.read_hooks()
        self.assertEqual(hooks["SessionStart"][-1], self.herdr_entry())
        self.assertNotIn("matcher", hooks["SessionStart"][-1])
        self.assertEqual(hooks["SessionStart"][:-1], base_hooks.get("SessionStart", []))
        for event in set(base_hooks) | set(hooks):
            if event != "SessionStart":
                self.assertEqual(hooks.get(event), base_hooks.get(event), event)
        self.assertEqual(absent.count(b"herdr-agent-state.sh"), 1)
        self.assertNotIn(b"agent-hooks", absent)

        self.set_launcher_env('HARNESS_PREFIX="x"')
        self.prepare()
        self.assertEqual(hooks_path.read_bytes(), baseline)
        self.install_herdr_script()
        self.set_launcher_env('HARNESS_PREFIX="x"', "export HARNESS_HERDR_AGENT_HOOKS='1'")
        self.prepare()
        self.assertEqual(hooks_path.read_bytes(), absent, "script presence must not change hooks.json")

    def launch_record_entry(self):
        python_bin = subprocess.run(
            ["/bin/bash", "-c", 'source "$1"; harness_python3_resolve', "_", str(ROOT / "bin" / "harness-common.sh")],
            env=self.environment(), text=True, capture_output=True, check=True,
        ).stdout.strip()
        command = f"{python_bin} {ROOT / 'bin' / 'harness-launch-record'} codex"
        return {"hooks": [{"type": "command", "command": command, "timeout": 5}]}

    def test_launch_record_opt_in_appends_one_python_session_start_entry(self):
        hooks_path = self.codex_home / "hooks.json"
        self.set_launcher_env('HARNESS_PREFIX="x"')
        self.prepare()
        baseline = hooks_path.read_bytes()
        base_hooks = json.loads(baseline)["hooks"]
        self.assertNotIn(b"harness-launch-record", baseline)
        # Ambient env never opts in.
        self.prepare(HARNESS_LAUNCH_RECORD_HOOKS="1")
        self.assertEqual(hooks_path.read_bytes(), baseline)

        self.set_launcher_env('HARNESS_PREFIX="x"', "HARNESS_LAUNCH_RECORD_HOOKS=1")
        self.prepare()
        hooks = self.read_hooks()
        self.assertEqual(hooks["SessionStart"][-1], self.launch_record_entry())
        self.assertNotIn("matcher", hooks["SessionStart"][-1])
        self.assertEqual(hooks["SessionStart"][:-1], base_hooks.get("SessionStart", []))
        for event in set(base_hooks) | set(hooks):
            if event != "SessionStart":
                self.assertEqual(hooks.get(event), base_hooks.get(event), event)
        self.assertEqual(hooks_path.read_bytes().count(b"harness-launch-record"), 1)
        # A flip regenerates; turning it off restores the exact baseline.
        self.set_launcher_env('HARNESS_PREFIX="x"', "HARNESS_LAUNCH_RECORD_HOOKS=0")
        self.prepare()
        self.assertEqual(hooks_path.read_bytes(), baseline)

    def test_launch_record_row_orders_after_orca_and_herdr(self):
        self.install_herdr_script()
        self.set_launcher_env("HARNESS_ORCA_AGENT_HOOKS=1", "HARNESS_HERDR_AGENT_HOOKS=1",
                              "HARNESS_LAUNCH_RECORD_HOOKS=1")
        self.prepare()
        tail = self.read_hooks()["SessionStart"][-3:]
        self.assertEqual(tail[0]["hooks"][0]["command"], self.orca_command())
        self.assertEqual(tail[1], self.herdr_entry())
        self.assertEqual(tail[2], self.launch_record_entry())

    def test_ssot_opt_in_generates_fail_open_stop_and_invalidates_warm_home(self):
        self.set_launcher_env('HARNESS_PREFIX="x"')
        self.prepare()
        baseline = self.read_hooks()
        optin = self.repo / 'config' / 'ssot-session-hooks.json'
        optin.write_text('{"enabled":true}')
        self.prepare()
        enabled = self.read_hooks()
        self.assertEqual(self.compiler_calls(), 2)
        for event in ('UserPromptSubmit', 'Stop'):
            self.assertEqual(enabled[event][:-1], baseline.get(event, []))
            command = enabled[event][-1]['hooks'][0]['command']
            self.assertIn('harness-session-hook', command)
            self.assertIn('--runtime codex', command)
            result = subprocess.run(command, shell=True, input='payload', text=True,
                                    capture_output=True, env={'HOME': str(self.home), 'PATH': '/usr/bin:/bin'})
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))
        callback = self.home / '.local/share/harness-service/bin/harness-session-hook'
        callback.parent.mkdir(parents=True)
        callback.write_text('#!/bin/sh\ncat > "$HOME/ssot-input"\necho bad-output\nexit 2\n')
        callback.chmod(0o755)
        command = enabled['Stop'][-1]['hooks'][0]['command']
        result = subprocess.run(command, shell=True, input='native-final-payload', text=True,
                                capture_output=True, env={'HOME': str(self.home), 'PATH': '/usr/bin:/bin'})
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))
        self.assertTrue((self.home / 'ssot-input').exists(), 'host callback was not invoked')
        self.assertEqual((self.home / 'ssot-input').read_text(), 'native-final-payload')
        self.prepare(HARNESS_SSOT_SESSION_HOOKS='0')
        self.assertEqual(self.compiler_calls(), 2)
        optin.unlink()
        self.prepare()
        self.assertEqual(self.read_hooks(), baseline)

    def test_orca_and_herdr_rows_are_ordered_after_harness_entries(self):
        # Case 4: six Orca entries first, then the herdr entry.
        events = ["SessionStart", "UserPromptSubmit", "PreToolUse",
                  "PermissionRequest", "PostToolUse", "Stop"]
        self.install_herdr_script()
        self.set_launcher_env('HARNESS_PREFIX="x"')
        self.prepare()
        base_hooks = json.loads((self.codex_home / "hooks.json").read_text(encoding="utf-8"))["hooks"]
        self.set_launcher_env("HARNESS_ORCA_AGENT_HOOKS=1", "HARNESS_HERDR_AGENT_HOOKS=1")
        self.prepare()
        hooks = self.read_hooks()
        orca_entry = {"hooks": [{"type": "command", "command": self.orca_command(), "timeout": 5}]}
        for event in events:
            expected_tail = [orca_entry] + ([self.herdr_entry()] if event == "SessionStart" else [])
            base = base_hooks.get(event, [])
            self.assertEqual(hooks[event], base + expected_tail, event)
        self.assertEqual(sum(1 for event in hooks for entry in hooks[event]
                             if "agent-hooks" in json.dumps(entry)), 6)
        self.assertEqual(sum(1 for event in hooks for entry in hooks[event]
                             if "herdr-agent-state" in json.dumps(entry)), 1)

    def test_herdr_opt_in_flip_regenerates_and_unchanged_run_stays_warm(self):
        # Case 5.
        hooks_path = self.codex_home / "hooks.json"
        self.set_launcher_env("HARNESS_HERDR_AGENT_HOOKS=0")
        self.prepare()
        self.assertEqual(self.compiler_calls(), 1)
        off = hooks_path.read_bytes()
        self.set_launcher_env("HARNESS_HERDR_AGENT_HOOKS=1")
        self.prepare()
        self.assertEqual(self.compiler_calls(), 2, "flipping the herdr opt-in did not regenerate")
        on = hooks_path.read_bytes()
        self.assertNotEqual(on, off)
        self.assertEqual(on.count(b"herdr-agent-state.sh"), 1)
        stamp_before = (self.codex_home / ".surface-success.json").read_bytes()
        for _ in range(2):
            self.prepare()
            self.assertEqual(self.compiler_calls(), 2, "unchanged run was not warm")
            self.assertEqual(hooks_path.read_bytes(), on)
        self.assertEqual((self.codex_home / ".surface-success.json").read_bytes(), stamp_before)
        # Each opt-in is independent: adding Orca on top regenerates once more.
        self.set_launcher_env("HARNESS_HERDR_AGENT_HOOKS=1", "HARNESS_ORCA_AGENT_HOOKS=1")
        self.prepare()
        self.assertEqual(self.compiler_calls(), 3)
        both = hooks_path.read_bytes()
        self.prepare()
        self.assertEqual(self.compiler_calls(), 3)
        self.assertEqual(hooks_path.read_bytes(), both)
        self.set_launcher_env("HARNESS_ORCA_AGENT_HOOKS=1")
        self.prepare()
        self.assertEqual(self.compiler_calls(), 4)
        self.assertNotIn(b"herdr-agent-state.sh", hooks_path.read_bytes())
        self.set_launcher_env("HARNESS_HERDR_AGENT_HOOKS=0")
        self.prepare()
        self.assertEqual(self.compiler_calls(), 5)
        self.assertEqual(hooks_path.read_bytes(), off)

    def test_herdr_hook_command_is_status_only_and_fail_open(self):
        # Case 6: run the exact generated command with a fixture HOME.
        self.set_launcher_env("HARNESS_ORCA_AGENT_HOOKS=1", "HARNESS_HERDR_AGENT_HOOKS=1")
        self.prepare()
        hooks = self.read_hooks()
        command = hooks["SessionStart"][-1]["hooks"][0]["command"]
        self.assertEqual(command, self.HERDR_COMMAND)
        # Status-only like the Orca row: the two commands differ only in the
        # script path and the argument.
        orca = hooks["SessionStart"][-2]["hooks"][0]["command"]
        self.assertEqual(orca, self.orca_command())
        self.assertEqual(
            orca.replace(".orca/agent-hooks/codex-hook.sh", ".codex/herdr-agent-state.sh")
                .replace('"$s" >/dev/null', '"$s" session >/dev/null'),
            command,
        )
        env = {"HOME": str(self.home), "PATH": "/usr/bin:/bin"}
        script = self.home / ".codex" / "herdr-agent-state.sh"
        args_seen = self.home / "args.seen"
        stdin_seen = self.home / "stdin.seen"

        def run():
            return subprocess.run(command, shell=True, input="{}", text=True,
                                  capture_output=True, env=env)

        # (a) script absent: exit 0, no stdout, no stderr.
        self.assertFalse(script.exists())
        result = run()
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        # (b) script prints to stdout and stderr: exit 0 and nothing surfaces.
        # It is invoked with `session` as its only argument and receives stdin.
        self.install_herdr_script(
            '#!/bin/sh\nprintf "%s\\n" "$#:$*" > "$HOME/args.seen"\ncat > "$HOME/stdin.seen"\n'
            'echo herdr-out\necho herdr-err >&2\n'
        )
        result = run()
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertEqual(args_seen.read_text(encoding="utf-8"), "1:session\n")
        self.assertEqual(stdin_seen.read_text(encoding="utf-8"), "{}")
        # (c) script exits 3: the failure never surfaces.
        args_seen.unlink()
        self.install_herdr_script('#!/bin/sh\nprintf "%s\\n" "$*" > "$HOME/args.seen"\necho oops >&2\nexit 3\n')
        result = run()
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertEqual(args_seen.read_text(encoding="utf-8"), "session\n")
        # (d) script present but not executable: skipped, exit 0, no output.
        script.chmod(0o644)
        args_seen.unlink()
        result = run()
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertFalse(args_seen.exists(), "a non-executable script must not run")

    def test_no_opt_in_hooks_json_matches_the_80cc12a_golden_output(self):
        # Independent oracle: the text below was captured from the generator at
        # 80cc12a (before the registry) for this fixture, with the machine
        # specific python path and bin directory replaced by placeholders. Any
        # formatting, ordering or content change to the no-opt-in output fails.
        golden = (
            '{\n'
            '  "hooks": {\n'
            '    "SessionStart": [\n'
            '      {\n'
            '        "hooks": [\n'
            '          {\n'
            '            "type": "command",\n'
            '            "command": "@PYTHON@ @BIN@/codex-cmux-title-sync.py",\n'
            '            "timeout": 3000\n'
            '          }\n'
            '        ]\n'
            '      }\n'
            '    ]\n'
            '  }\n'
            '}\n'
        )
        python_bin = subprocess.run(
            ["/bin/bash", "-c", 'source "$1"; harness_python3_resolve', "_", str(ROOT / "bin" / "harness-common.sh")],
            env=self.environment(), text=True, capture_output=True, check=True,
        ).stdout.strip()
        expected = golden.replace("@PYTHON@", python_bin).replace("@BIN@", str(ROOT / "bin"))
        self.install_herdr_script()
        hooks_path = self.codex_home / "hooks.json"
        # No launcher.env at all, then explicit off values for both runtimes.
        self.prepare()
        self.assertEqual(hooks_path.read_text(encoding="utf-8"), expected)
        self.set_launcher_env("HARNESS_ORCA_AGENT_HOOKS=0", "HARNESS_HERDR_AGENT_HOOKS=0")
        self.prepare()
        self.assertEqual(hooks_path.read_text(encoding="utf-8"), expected)
        # Process environment alone never changes it, even on a cold rebuild.
        (self.codex_home / ".surface-success.json").unlink()
        self.prepare(HARNESS_ORCA_AGENT_HOOKS="1", HARNESS_HERDR_AGENT_HOOKS="1")
        self.assertEqual(hooks_path.read_text(encoding="utf-8"), expected)

    def test_profile_flags_and_warm_fingerprint_invalidation(self):
        # Installed plugins carry tests/docs/assets that are not copied into
        # the generated surface. Keep a representative payload while treating
        # timing as host-sensitive metadata; compiler calls are the warm oracle.
        plugin_noise = self.plugin_skill.parents[2] / "tests" / "payload"
        plugin_noise.mkdir(parents=True)
        for index in range(1200):
            (plugin_noise / f"case-{index}.txt").write_text("noise\n", encoding="utf-8")
        self.prepare()
        self.assertEqual(self.compiler_calls(), 1)
        self.assertTrue(self.enabled_value("context7"))
        self.assertTrue(self.enabled_value("harness-rag"))
        self.assertFalse(self.enabled_value("jira"))
        self.assertFalse(self.plugin_enabled("computer-use"))
        self.assertFalse(self.plugin_enabled("chrome"))
        self.assertTrue((self.codex_home / ".surface-success.json").is_file())
        fingerprint_cache = json.loads(
            (self.codex_home / ".surface-fingerprint-cache.json").read_text(encoding="utf-8")
        )
        title_sync = os.path.realpath(ROOT / "bin" / "codex-cmux-title-sync.py")
        self.assertIn(
            title_sync,
            fingerprint_cache.get("files", {}),
            "cmux title helper is missing from the launcher-owned fingerprint",
        )
        pretool_adapter = os.path.realpath(ROOT / "bin" / "codex-pretool-adapter.py")
        self.assertIn(
            pretool_adapter,
            fingerprint_cache.get("files", {}),
            "strict PreToolUse adapter is missing from the launcher-owned fingerprint",
        )

        self.assertIn(
            os.path.realpath(ROOT / "bin" / "slack-approval-policy.py"),
            fingerprint_cache.get("files", {}),
            "Slack policy helper is missing from the launcher-owned fingerprint",
        )
        warm_samples = []
        for _ in range(5):
            started = time.perf_counter()
            self.prepare()
            warm_samples.append(time.perf_counter() - started)
        elapsed = statistics.median(warm_samples)
        print(f"WARM_PREPARE_MEDIAN_MS={elapsed * 1000:.1f}")
        self.assertEqual(self.compiler_calls(), 1, "warm prepare reran the compiler")

        # Runtime/auth state is deliberately outside the input fingerprint.
        (self.home / ".codex").mkdir(exist_ok=True)
        (self.home / ".codex" / "auth.json").write_text('{"token":"changed"}\n', encoding="utf-8")
        (self.codex_home / "sessions").mkdir()
        (self.codex_home / "sessions" / "one.jsonl").write_text("session\n", encoding="utf-8")
        with (self.codex_home / "config.toml").open("a", encoding="utf-8") as stream:
            stream.write("\n[hooks.state]\nenabled = false\n")
        self.prepare()
        self.assertEqual(self.compiler_calls(), 1)

        # Every launcher-owned source class must invalidate independently.
        alpha = self.repo / ".claude" / "skills" / "alpha" / "SKILL.md"
        alpha.write_text(alpha.read_text(encoding="utf-8") + "source change\n", encoding="utf-8")
        self.prepare()
        self.assertEqual(self.compiler_calls(), 2)

        mcp = json.loads(self.mcp_path.read_text(encoding="utf-8"))
        mcp["mcpServers"]["unused"] = {"command": "unused"}
        self.mcp_path.write_text(json.dumps(mcp, indent=2) + "\n", encoding="utf-8")
        self.prepare()
        self.assertEqual(self.compiler_calls(), 3)
        self.assertFalse(self.enabled_value("unused"))

        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        manifest["repo"] = "fixture-renamed"
        self.manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        self.prepare()
        self.assertEqual(self.compiler_calls(), 4)

        self.plugin_skill.write_text(
            self.plugin_skill.read_text(encoding="utf-8") + "plugin source change\n", encoding="utf-8"
        )
        self.prepare()
        self.assertEqual(self.compiler_calls(), 5)

        catalog = json.loads((self.codex_home / "skill-catalog.json").read_text(encoding="utf-8"))
        alpha_link = Path(next(item for item in catalog["skills"] if item["name"] == "alpha")["exposed_path"]).parent
        alpha_link.unlink()
        self.prepare()
        self.assertEqual(self.compiler_calls(), 6)

        self.prepare(HARNESS_CODEX_MCP_PROFILE="work")
        self.assertEqual(self.compiler_calls(), 7)
        self.assertTrue(self.enabled_value("jira"))
        self.assertTrue(self.plugin_enabled("computer-use"))
        self.assertFalse(self.plugin_enabled("chrome"))
        catalog = json.loads((self.codex_home / "skill-catalog.json").read_text(encoding="utf-8"))
        self.assertEqual(catalog["mcp"]["profile"], "work")
        self.assertIn(
            "computer-use:computer-use",
            {item["name"] for item in catalog["skills"]},
        )

    def test_global_allowlist_definitions_follow_exact_profiles_and_warm_digest(self):
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        manifest["mcp"]["definition_sources"].append("codex-global-allowlist")
        manifest["mcp"]["profiles"]["work"]["enabled"].append("global-tool")
        self.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        global_config = self.home / ".codex" / "config.toml"
        global_config.parent.mkdir(exist_ok=True)
        global_config.write_text(
            '[mcp_servers.global-tool]\ncommand = "global-v1"\nenabled = true\n\n'
            '[mcp_servers.unselected]\ncommand = "unselected-v1"\n',
            encoding="utf-8",
        )

        self.prepare(HARNESS_CODEX_GLOBAL_MCP_ALLOWLIST="global-tool")

        with (self.codex_home / "config.toml").open("rb") as stream:
            config = tomllib.load(stream)
        self.assertEqual(config["mcp_servers"]["global-tool"]["command"], "global-v1")
        self.assertFalse(config["mcp_servers"]["global-tool"]["enabled"])
        self.assertNotIn("unselected", config["mcp_servers"])
        self.assertEqual(self.compiler_calls(), 1)

        self.prepare(HARNESS_CODEX_GLOBAL_MCP_ALLOWLIST="global-tool")
        self.assertEqual(self.compiler_calls(), 1, "unchanged selected global MCP was not warm")

        global_config.write_text(
            '[mcp_servers.global-tool]\ncommand = "global-v1"\nenabled = true\n\n'
            '[mcp_servers.unselected]\ncommand = "unselected-v2"\n',
            encoding="utf-8",
        )
        self.prepare(HARNESS_CODEX_GLOBAL_MCP_ALLOWLIST="global-tool")
        self.assertEqual(self.compiler_calls(), 1, "unselected global MCP invalidated warm prepare")

        self.prepare(HARNESS_CODEX_GLOBAL_MCP_ALLOWLIST="global-tool,unselected")
        self.assertEqual(self.compiler_calls(), 2, "allowlist membership mutation stayed warm")

        global_config.write_text(
            '[mcp_servers.global-tool]\ncommand = "global-v2"\nenabled = true\n\n'
            '[mcp_servers.unselected]\ncommand = "unselected-v2"\n',
            encoding="utf-8",
        )
        self.prepare(HARNESS_CODEX_GLOBAL_MCP_ALLOWLIST="global-tool")
        self.assertEqual(self.compiler_calls(), 3)

        self.prepare(
            HARNESS_CODEX_GLOBAL_MCP_ALLOWLIST="global-tool",
            HARNESS_CODEX_MCP_PROFILE="work",
        )
        self.assertEqual(self.compiler_calls(), 4)
        self.assertTrue(self.enabled_value("global-tool"))

    def test_apps_allowlist_changes_rebuild_once_and_revoke_permissions(self):
        self.prepare()
        self.assertEqual(self.compiler_calls(), 1)
        with (self.codex_home / "config.toml").open("rb") as stream:
            disabled = tomllib.load(stream)
        self.assertFalse(disabled["features"]["apps"])
        self.assertNotIn("apps", disabled)

        runtime = {
            "auth.json": "local-auth\n",
            "plugins/cache/user-runtime/state.json": "runtime-plugin\n",
        }
        for relative, content in runtime.items():
            path = self.codex_home / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        runtime_before = self.snapshot_paths(runtime)
        with (self.codex_home / "config.toml").open("a", encoding="utf-8") as stream:
            stream.write(
                '\n[hooks.state."app-trust"]\ntrusted_hash = "preserved"\n'
                '\n[[skills.config]]\npath = "/tmp/external-skill"\nenabled = false\n'
                '\n[marketplaces.external]\nsource_type = "local"\nsource = "/tmp/external"\n'
                '\n[plugins."external@marketplace"]\nenabled = true\n'
            )

        def assert_runtime_preserved():
            self.assertEqual(self.snapshot_paths(runtime), runtime_before)
            with (self.codex_home / "config.toml").open("rb") as stream:
                config = tomllib.load(stream)
            self.assertEqual(config["hooks"]["state"]["app-trust"], {"trusted_hash": "preserved"})
            self.assertIn({"path": "/tmp/external-skill", "enabled": False}, config["skills"]["config"])
            self.assertEqual(config["marketplaces"]["external"]["source"], "/tmp/external")
            self.assertTrue(config["plugins"]["external@marketplace"]["enabled"])

        self.prepare(HARNESS_CODEX_APPS_ALLOWLIST="asdk_app_alpha,connector_calendar,templated_apps_fixture")
        self.assertEqual(self.compiler_calls(), 2)
        assert_runtime_preserved()
        with (self.codex_home / "config.toml").open("rb") as stream:
            enabled = tomllib.load(stream)
        self.assertTrue(enabled["features"]["apps"])
        self.assertEqual(
            enabled["apps"],
            {
                "_default": {"enabled": False},
                "asdk_app_alpha": {"enabled": True},
                "connector_calendar": {"enabled": True},
                "templated_apps_fixture": {"enabled": True},
            },
        )
        self.prepare(HARNESS_CODEX_APPS_ALLOWLIST="asdk_app_alpha,connector_calendar,templated_apps_fixture")
        self.assertEqual(self.compiler_calls(), 2, "unchanged app allowlist was not warm")

        with (self.codex_home / "config.toml").open("a", encoding="utf-8") as stream:
            stream.write('\n[apps.asdk_app_rogue]\nenabled = true\n')
        self.prepare(HARNESS_CODEX_APPS_ALLOWLIST="asdk_app_alpha,connector_calendar,templated_apps_fixture")
        self.assertEqual(self.compiler_calls(), 3, "unlisted app bypass stayed warm")
        assert_runtime_preserved()
        with (self.codex_home / "config.toml").open("rb") as stream:
            repaired = tomllib.load(stream)
        self.assertEqual(
            repaired["apps"],
            {
                "_default": {"enabled": False},
                "asdk_app_alpha": {"enabled": True},
                "connector_calendar": {"enabled": True},
                "templated_apps_fixture": {"enabled": True},
            },
        )

        self.prepare(HARNESS_CODEX_APPS_ALLOWLIST="asdk_app_beta")
        self.assertEqual(self.compiler_calls(), 4)
        assert_runtime_preserved()
        with (self.codex_home / "config.toml").open("rb") as stream:
            changed = tomllib.load(stream)
        self.assertEqual(
            changed["apps"],
            {
                "_default": {"enabled": False},
                "asdk_app_beta": {"enabled": True},
            },
        )

        self.prepare()
        self.assertEqual(self.compiler_calls(), 5)
        assert_runtime_preserved()
        with (self.codex_home / "config.toml").open("rb") as stream:
            revoked = tomllib.load(stream)
        self.assertFalse(revoked["features"]["apps"])
        self.assertNotIn("apps", revoked)
        self.prepare()
        self.assertEqual(self.compiler_calls(), 5, "revoked app allowlist was not warm")

        # The private Slack policy is an independent identity axis: same app
        # allowlist, changed approval policy must rebuild, then stay warm.
        self.prepare(HARNESS_CODEX_APPS_ALLOWLIST="asdk_app_beta", HARNESS_CODEX_SLACK_APPS="asdk_app_beta")
        self.assertEqual(self.compiler_calls(), 6)
        with (self.codex_home / "config.toml").open("rb") as stream:
            slack = tomllib.load(stream)["apps"]["asdk_app_beta"]
        self.assertEqual(slack["approvals_reviewer"], "user")
        self.assertEqual(slack["tools"]["slack_slack_send_message"]["approval_mode"], "prompt")
        self.prepare(HARNESS_CODEX_APPS_ALLOWLIST="asdk_app_beta", HARNESS_CODEX_SLACK_APPS="asdk_app_beta")
        self.assertEqual(self.compiler_calls(), 6, "Slack approval policy did not stay warm")
        self.prepare(HARNESS_CODEX_APPS_ALLOWLIST="asdk_app_beta")
        self.assertEqual(self.compiler_calls(), 7, "removed Slack policy stayed warm")
        self.prepare(HARNESS_CODEX_APPS_ALLOWLIST="asdk_app_beta")
        self.assertEqual(self.compiler_calls(), 7)
        assert_runtime_preserved()


    def test_global_allowlist_emits_exact_profile_policies_without_source_drift(self):
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        manifest["mcp"]["definition_sources"].append("codex-global-allowlist")
        for profile in ("default", "work"):
            manifest["mcp"]["profiles"][profile]["enabled"].append("global-tool")
        manifest["mcp"]["profiles"]["default"]["policies"] = {
            "global-tool": {"enabled_tools": ["default-search"]}
        }
        manifest["mcp"]["profiles"]["work"]["policies"] = {
            "global-tool": {"disabled_tools": ["work-delete"]}
        }
        self.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        global_config = self.home / ".codex" / "config.toml"
        global_config.parent.mkdir(exist_ok=True)
        global_config.write_text(
            '[mcp_servers.global-tool]\ncommand = "global-command"\nargs = ["serve"]\n',
            encoding="utf-8",
        )

        self.prepare(HARNESS_CODEX_GLOBAL_MCP_ALLOWLIST="global-tool")

        catalog = json.loads((self.codex_home / "skill-catalog.json").read_text())
        with (self.codex_home / "config.toml").open("rb") as stream:
            default_config = tomllib.load(stream)["mcp_servers"]["global-tool"]
        self.assertEqual(
            catalog["mcp"]["policies"],
            {"global-tool": {"enabled_tools": ["default-search"]}},
        )
        self.assertEqual(default_config["enabled_tools"], ["default-search"])
        self.assertNotIn("disabled_tools", default_config)
        self.assertEqual(
            GLOBAL_MCP_MODULE.definition_projection(default_config, self.home),
            GLOBAL_MCP_MODULE.definition_projection(
                {"command": "global-command", "args": ["serve"]}, self.home
            ),
        )

        self.prepare(
            HARNESS_CODEX_GLOBAL_MCP_ALLOWLIST="global-tool",
            HARNESS_CODEX_MCP_PROFILE="work",
        )

        catalog = json.loads((self.codex_home / "skill-catalog.json").read_text())
        with (self.codex_home / "config.toml").open("rb") as stream:
            work_config = tomllib.load(stream)["mcp_servers"]["global-tool"]
        self.assertEqual(
            catalog["mcp"]["policies"],
            {"global-tool": {"disabled_tools": ["work-delete"]}},
        )
        self.assertEqual(work_config["disabled_tools"], ["work-delete"])
        self.assertNotIn("enabled_tools", work_config)
        self.assertEqual(
            GLOBAL_MCP_MODULE.definition_projection(work_config, self.home),
            GLOBAL_MCP_MODULE.definition_projection(
                {"command": "global-command", "args": ["serve"]}, self.home
            ),
        )
        calls = self.compiler_calls()
        self.prepare(
            HARNESS_CODEX_GLOBAL_MCP_ALLOWLIST="global-tool",
            HARNESS_CODEX_MCP_PROFILE="work",
        )
        self.assertEqual(self.compiler_calls(), calls, "matching policy fields did not stay warm")

    def test_local_mcp_runtime_policy_renders_and_stays_warm(self):
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        manifest["mcp"]["profiles"]["default"]["policies"] = {
            "context7": {
                "startup_timeout_sec": 10,
                "tool_timeout_sec": 45,
                "required": True,
                "default_tools_approval_mode": "approve",
                "tools": {
                    "resolve-library-id": {"approval_mode": "approve"},
                },
            }
        }
        self.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        self.prepare()

        with (self.codex_home / "config.toml").open("rb") as stream:
            server_config = tomllib.load(stream)["mcp_servers"]["context7"]
        self.assertEqual(server_config["startup_timeout_sec"], 10)
        self.assertEqual(server_config["tool_timeout_sec"], 45)
        self.assertIs(server_config["required"], True)
        self.assertEqual(server_config["default_tools_approval_mode"], "approve")
        self.assertEqual(
            server_config["tools"]["resolve-library-id"]["approval_mode"],
            "approve",
        )
        calls = self.compiler_calls()
        self.prepare()
        self.assertEqual(self.compiler_calls(), calls)

    def test_selected_global_definition_edits_invalidate_or_fail_closed(self):
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        manifest["mcp"]["definition_sources"].append("codex-global-allowlist")
        manifest["mcp"]["profiles"]["default"]["enabled"].append("global-tool")
        self.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        global_config = self.home / ".codex" / "config.toml"
        global_config.parent.mkdir(exist_ok=True)
        valid = '[mcp_servers.global-tool]\ncommand = "global-v1"\n'
        global_config.write_text(valid, encoding="utf-8")
        self.prepare(HARNESS_CODEX_GLOBAL_MCP_ALLOWLIST="global-tool")

        global_config.write_text(valid + 'args = ["serve"]\n', encoding="utf-8")
        self.prepare(HARNESS_CODEX_GLOBAL_MCP_ALLOWLIST="global-tool")
        self.assertEqual(self.compiler_calls(), 2, "selected definition addition stayed warm")

        global_config.write_text(valid.replace("v1", "v2"), encoding="utf-8")
        self.prepare(HARNESS_CODEX_GLOBAL_MCP_ALLOWLIST="global-tool")
        self.assertEqual(self.compiler_calls(), 3, "selected definition change stayed warm")

        stamp = self.codex_home / ".surface-success.json"
        for broken in ("", ' [mcp_servers.global-tool]\nenabled = false\n', "[mcp_servers.global-tool\n"):
            with self.subTest(broken=broken):
                global_config.write_text(valid, encoding="utf-8")
                self.prepare(HARNESS_CODEX_GLOBAL_MCP_ALLOWLIST="global-tool")
                self.assertTrue(stamp.exists())
                global_config.write_text(broken, encoding="utf-8")
                result = subprocess.run(
                    ["/bin/bash", str(PREPARE), str(self.repo)],
                    env=self.environment(HARNESS_CODEX_GLOBAL_MCP_ALLOWLIST="global-tool"),
                    text=True,
                    capture_output=True,
                )
                self.assertNotEqual(result.returncode, 0, result.stderr)
                self.assertTrue(stamp.exists(), "failed global resolution removed the prior warm stamp")

    def test_fixture_environment_removes_inherited_surface_overrides(self):
        with mock.patch.dict(
            os.environ,
            {
                "HARNESS_CODEX_MCP_PROFILE": "work",
                "HARNESS_CODEX_SKILL_PROFILE": "design",
                "HARNESS_CODEX_GLOBAL_MCP_ALLOWLIST": "inherited-only",
            },
        ):
            self.prepare()

        catalog = json.loads((self.codex_home / "skill-catalog.json").read_text())
        self.assertEqual(catalog["mcp"]["profile"], "default")
        self.assertFalse((self.home / ".codex" / "config.toml").exists())

    def test_nonlogin_system_python_path_falls_back_to_homebrew_python(self):
        if not any(
            path.is_file()
            for path in (
                Path("/opt/homebrew/opt/python@3.13/libexec/bin/python3"),
                Path("/usr/local/opt/python@3.13/libexec/bin/python3"),
            )
        ):
            self.skipTest("Homebrew Python path is not present")

        self.prepare(PATH="/usr/bin:/bin")

        self.assertEqual(self.compiler_calls(), 1)
        self.assertTrue((self.codex_home / ".surface-success.json").is_file())

    def test_curated_metadata_rewrite_stays_warm_but_new_version_invalidates(self):
        curated = (
            self.codex_home
            / "plugins"
            / "cache"
            / "openai-curated-remote"
            / "superpowers"
        )
        write_skill(
            curated / "1.0.0" / "skills",
            "curated-one",
            "superpowers:curated-one",
            "curated v1",
        )
        metadata = curated / ".codex-remote-plugin-install.json"
        metadata.write_text('{"schema_version":1}\n', encoding="utf-8")
        self.prepare()

        metadata.write_text('{"schema_version":1}\n', encoding="utf-8")
        self.prepare()
        self.assertEqual(
            self.compiler_calls(),
            1,
            "curated metadata-only rewrite invalidated the warm surface",
        )

        write_skill(
            curated / "2.0.0" / "skills",
            "curated-two",
            "superpowers:curated-two",
            "curated v2",
        )
        self.prepare()
        self.assertEqual(self.compiler_calls(), 2)

    def test_existing_gpt6_profiles_survive_warm_prepare_and_repair_drift(self):
        self.prepare()
        fast = self.codex_home / "fast.config.toml"
        sol = self.codex_home / "sol.config.toml"
        self.assertEqual(tomllib.loads(fast.read_text())["model"], "gpt-6-luna")
        self.assertEqual(tomllib.loads(sol.read_text())["model"], "gpt-6.1-sol")
        self.assertFalse((self.codex_home / "luna6.config.toml").exists())
        self.assertFalse((self.codex_home / "sol6.config.toml").exists())
        profile = self.codex_home / "astra.config.toml"
        expected = {"model": "gpt-6-astra", "model_reasoning_effort": "medium"}
        self.assertEqual(tomllib.loads(profile.read_text()), expected)
        before = profile.stat().st_mtime_ns
        self.prepare()
        self.assertEqual(self.compiler_calls(), 1)
        self.assertEqual(profile.stat().st_mtime_ns, before)
        (self.codex_home / "luna6.config.toml").write_text('model = "gpt-6-luna"\n')
        (self.codex_home / "sol6.config.toml").write_text('model = "gpt-6-sol"\n')
        sol.unlink()
        self.prepare()
        self.assertEqual(self.compiler_calls(), 2)
        self.assertEqual(tomllib.loads(sol.read_text())["model"], "gpt-6.1-sol")
        self.assertFalse((self.codex_home / "luna6.config.toml").exists())
        self.assertFalse((self.codex_home / "sol6.config.toml").exists())
        profile.write_text('model = "wrong-model"\n')
        self.prepare()
        self.assertEqual(self.compiler_calls(), 3)
        self.assertEqual(tomllib.loads(profile.read_text()), expected)
        profile.unlink()
        self.prepare()
        self.assertEqual(self.compiler_calls(), 4)
        self.assertEqual(tomllib.loads(profile.read_text()), expected)
        self.prepare()
        self.assertEqual(self.compiler_calls(), 4)

    def test_preflight_failure_preserves_every_managed_output(self):
        self.prepare()
        managed = self.managed_output_paths()
        self.assertEqual(
            managed,
            [
                ".surface-fingerprint-cache.json",
                ".surface-success.json",
                "AGENTS.md",
                "agents/.harness-managed",
                "astra.config.toml",
                "base.config.toml",
                "config.toml",
                "fast.config.toml",
                "hooks.json",
                "luna6.config.toml",
                "plan.config.toml",
                "plugins/cache/openai-bundled/browser",
                "plugins/cache/openai-bundled/chrome",
                "plugins/cache/openai-bundled/computer-use",
                "rich.config.toml",
                "skill-catalog.json",
                "skills/.harness-managed",
                "skills/.harness-managed-cmds",
                "skills/alpha",
                "skills/brainstorming",
                "skills/project-explicit",
                "sol.config.toml",
                "sol6.config.toml",
                "surface.config.toml",
            ],
        )
        before = self.snapshot_paths(managed)
        local_mcp = self.repo / ".mcp.local.json"
        local_mcp.write_text(
            json.dumps({"mcpServers": {"context7": {"command": "duplicate"}}}) + "\n",
            encoding="utf-8",
        )
        self.prepare(expect=2)
        self.assertEqual(self.snapshot_paths(managed), before)
        self.assertEqual(self.staging_artifacts(), [])

    def test_candidate_failure_and_signal_preserve_managed_and_runtime_state(self):
        self.prepare()
        managed = self.managed_output_paths()
        managed_before = self.snapshot_paths(managed)
        runtime = {
            "sessions/one.jsonl": "session\n",
            "history.jsonl": "history\n",
            "auth.json": "local-auth\n",
            "plugins/cache/user-runtime/state.json": "runtime-plugin\n",
            "skills/user-owned/SKILL.md": "---\nname: user-owned\n---\n",
        }
        for relative, content in runtime.items():
            path = self.codex_home / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        runtime_before = self.snapshot_paths(runtime)
        alpha = self.repo / ".claude" / "skills" / "alpha" / "SKILL.md"
        alpha.write_text(alpha.read_text(encoding="utf-8") + "candidate rebuild\n", encoding="utf-8")

        self.prepare(expect=23, HARNESS_TEST_COMPILER_FAIL="1")
        self.assertEqual(self.snapshot_paths(managed), managed_before)
        self.assertEqual(self.snapshot_paths(runtime), runtime_before)
        self.assertEqual(self.staging_artifacts(), [])

        self.prepare(expect=86, HARNESS_TEST_CODEX_PREPARE_FAIL_AFTER_CANDIDATE="1")
        self.assertEqual(self.snapshot_paths(managed), managed_before)
        self.assertEqual(self.snapshot_paths(runtime), runtime_before)
        self.assertEqual(self.staging_artifacts(), [])

        process = subprocess.Popen(
            ["/bin/bash", str(PREPARE), str(self.repo)],
            env=self.environment(HARNESS_TEST_CODEX_PREPARE_PAUSE_AFTER_CANDIDATE="1"),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.wait_for_condition(
            process,
            lambda: any(
                path.joinpath(".candidate-ready").exists()
                for path in self.staging_artifacts()
            ),
            "candidate pause",
        )
        process.terminate()
        process.communicate(timeout=COORDINATION_TIMEOUT_SECONDS)
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(self.snapshot_paths(managed), managed_before)
        self.assertEqual(self.snapshot_paths(runtime), runtime_before)
        self.assertEqual(self.staging_artifacts(), [])

    def test_success_publishes_managed_outputs_without_touching_runtime_state(self):
        self.prepare()
        runtime = {
            "sessions/one.jsonl": "session\n",
            "history.jsonl": "history\n",
            "auth.json": "local-auth\n",
            "plugins/cache/user-runtime/state.json": "runtime-plugin\n",
        }
        for relative, content in runtime.items():
            path = self.codex_home / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        before = self.snapshot_paths(runtime)
        alpha = self.repo / ".claude" / "skills" / "alpha" / "SKILL.md"
        alpha.write_text(alpha.read_text(encoding="utf-8") + "successful rebuild\n", encoding="utf-8")

        self.prepare()

        self.assertEqual(self.snapshot_paths(runtime), before)
        self.assertEqual(self.staging_artifacts(), [])

    def test_publish_failure_rolls_back_every_managed_output(self):
        self.prepare()
        managed = self.managed_output_paths()
        before = self.snapshot_paths(managed)
        alpha = self.repo / ".claude" / "skills" / "alpha" / "SKILL.md"
        alpha.write_text(alpha.read_text(encoding="utf-8") + "publish failure\n", encoding="utf-8")

        self.prepare(expect=1, HARNESS_TEST_CODEX_PREPARE_FAIL_DURING_PUBLISH="1")

        self.assertEqual(self.snapshot_paths(managed), before)
        self.assertEqual(self.staging_artifacts(), [])

    def test_concurrent_runtime_config_write_is_merged_before_publish(self):
        self.prepare()
        alpha = self.repo / ".claude" / "skills" / "alpha" / "SKILL.md"
        alpha.write_text(alpha.read_text(encoding="utf-8") + "config race\n", encoding="utf-8")
        ready = self.tmp / "config-merge-ready"
        release = self.tmp / "config-merge-release"
        process = self.start_prepare(
            HARNESS_TEST_CODEX_CONFIG_MERGE_READY=str(ready),
            HARNESS_TEST_CODEX_CONFIG_MERGE_RELEASE=str(release),
        )
        self.wait_for_condition(process, ready.exists, f"gate {ready}")
        with (self.codex_home / "config.toml").open("a", encoding="utf-8") as stream:
            stream.write(
                '\n[hooks.state."concurrent-hook"]\n'
                'trusted_hash = "concurrent"\n'
                '\n[[skills.config]]\n'
                'path = "/tmp/concurrent-skill"\n'
                'enabled = false\n'
                '\n[marketplaces.concurrent-external]\n'
                'source_type = "local"\n'
                'source = "/tmp/concurrent-marketplace"\n'
                '\n[plugins."concurrent@external"]\n'
                'enabled = true\n'
            )
        release.touch()
        stdout, stderr = process.communicate(timeout=COORDINATION_TIMEOUT_SECONDS)
        self.assertEqual(process.returncode, 0, f"stdout={stdout}\nstderr={stderr}")

        with (self.codex_home / "config.toml").open("rb") as stream:
            config = tomllib.load(stream)
        self.assertEqual(config["hooks"]["state"]["concurrent-hook"]["trusted_hash"], "concurrent")
        self.assertIn(
            {"path": "/tmp/concurrent-skill", "enabled": False},
            config["skills"]["config"],
        )
        self.assertEqual(
            config["marketplaces"]["concurrent-external"]["source"],
            "/tmp/concurrent-marketplace",
        )
        self.assertTrue(config["plugins"]["concurrent@external"]["enabled"])

    def test_invalid_live_config_fails_preflight_before_staging(self):
        self.prepare()
        managed = self.managed_output_paths()
        before = self.snapshot_paths(managed)
        (self.codex_home / "config.toml").write_text("invalid = [\n", encoding="utf-8")
        invalid_before = (self.codex_home / "config.toml").read_bytes()
        alpha = self.repo / ".claude" / "skills" / "alpha" / "SKILL.md"
        alpha.write_text(alpha.read_text(encoding="utf-8") + "invalid config\n", encoding="utf-8")

        result = self.prepare(expect=2)

        self.assertIn("live Codex config", result.stderr)
        self.assertEqual((self.codex_home / "config.toml").read_bytes(), invalid_before)
        self.assertEqual(
            self.snapshot_paths(path for path in managed if path != "config.toml"),
            {key: value for key, value in before.items() if key != "config.toml"},
        )
        self.assertEqual(self.staging_artifacts(), [])

    def test_fresh_home_gets_auth_symlink_without_managing_auth(self):
        global_auth = self.home / ".codex" / "auth.json"
        global_auth.parent.mkdir(parents=True)
        global_auth.write_text("global-auth\n", encoding="utf-8")

        self.prepare()

        auth = self.codex_home / "auth.json"
        self.assertTrue(auth.is_symlink())
        self.assertEqual(os.readlink(auth), str(global_auth))
        self.assertNotIn("auth.json", self.managed_output_paths())

    def test_existing_auth_file_is_never_overwritten_on_warm_prepare(self):
        global_auth = self.home / ".codex" / "auth.json"
        global_auth.parent.mkdir(parents=True)
        global_auth.write_text("global-auth\n", encoding="utf-8")
        self.prepare()
        auth = self.codex_home / "auth.json"
        if auth.exists() or auth.is_symlink():
            auth.unlink()
        auth.write_text("existing-local-auth\n", encoding="utf-8")

        self.prepare()

        self.assertFalse(auth.is_symlink())
        self.assertEqual(auth.read_text(encoding="utf-8"), "existing-local-auth\n")

    def test_concurrent_auth_create_wins_post_publish_repair(self):
        global_auth = self.home / ".codex" / "auth.json"
        global_auth.parent.mkdir(parents=True)
        global_auth.write_text("global-auth\n", encoding="utf-8")
        self.prepare()
        auth = self.codex_home / "auth.json"
        if auth.exists() or auth.is_symlink():
            auth.unlink()
        alpha = self.repo / ".claude" / "skills" / "alpha" / "SKILL.md"
        alpha.write_text(alpha.read_text(encoding="utf-8") + "auth race\n", encoding="utf-8")
        ready = self.tmp / "auth-repair-ready"
        release = self.tmp / "auth-repair-release"
        process = self.start_prepare(
            HARNESS_TEST_CODEX_AUTH_REPAIR_READY=str(ready),
            HARNESS_TEST_CODEX_AUTH_REPAIR_RELEASE=str(release),
        )
        self.wait_for_condition(process, ready.exists, f"gate {ready}")
        auth.write_text("concurrent-auth\n", encoding="utf-8")
        release.touch()
        stdout, stderr = process.communicate(timeout=COORDINATION_TIMEOUT_SECONDS)

        self.assertEqual(process.returncode, 0, f"stdout={stdout}\nstderr={stderr}")
        self.assertFalse(auth.is_symlink())
        self.assertEqual(auth.read_text(encoding="utf-8"), "concurrent-auth\n")

    def test_local_publish_failure_rolls_back_global_marketplace(self):
        self.prepare()
        global_marketplace = (
            self.home / ".codex" / ".tmp" / "bundled-marketplaces" / "openai-bundled"
        )
        before = self.snapshot_tree(global_marketplace)
        source_skill = (
            self.no_marketplace
            / "plugins"
            / "computer-use"
            / "skills"
            / "computer-use"
            / "SKILL.md"
        )
        source_skill.write_text(
            source_skill.read_text(encoding="utf-8") + "global transaction update\n",
            encoding="utf-8",
        )
        alpha = self.repo / ".claude" / "skills" / "alpha" / "SKILL.md"
        alpha.write_text(alpha.read_text(encoding="utf-8") + "global rollback\n", encoding="utf-8")

        self.prepare(expect=1, HARNESS_TEST_CODEX_PREPARE_FAIL_DURING_PUBLISH="1")

        self.assertEqual(self.snapshot_tree(global_marketplace), before)
        self.assertEqual(
            list(global_marketplace.parent.glob(".openai-bundled-candidate.*")), []
        )

    def test_signal_after_global_exchange_rolls_back_global_marketplace(self):
        self.prepare()
        global_marketplace = (
            self.home / ".codex" / ".tmp" / "bundled-marketplaces" / "openai-bundled"
        )
        before = self.snapshot_tree(global_marketplace)
        source_skill = (
            self.no_marketplace
            / "plugins"
            / "computer-use"
            / "skills"
            / "computer-use"
            / "SKILL.md"
        )
        source_skill.write_text(
            source_skill.read_text(encoding="utf-8") + "global signal update\n",
            encoding="utf-8",
        )
        alpha = self.repo / ".claude" / "skills" / "alpha" / "SKILL.md"
        alpha.write_text(alpha.read_text(encoding="utf-8") + "global signal\n", encoding="utf-8")
        ready = self.tmp / "global-publish-ready"
        release = self.tmp / "global-publish-release"
        process = self.start_prepare(
            start_new_session=True,
            HARNESS_TEST_CODEX_GLOBAL_PUBLISH_READY=str(ready),
            HARNESS_TEST_CODEX_GLOBAL_PUBLISH_RELEASE=str(release),
        )
        self.wait_for_condition(process, ready.exists, f"gate {ready}")
        os.killpg(process.pid, signal.SIGTERM)
        process.communicate(timeout=COORDINATION_TIMEOUT_SECONDS)

        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(self.snapshot_tree(global_marketplace), before)
        self.assertEqual(
            list(global_marketplace.parent.glob(".openai-bundled-candidate.*")), []
        )

    def test_failure_after_first_of_two_quarantines_restores_both_conflicts(self):
        self.prepare()
        rogue_skill = write_skill(
            self.codex_home / "skills",
            "first-rogue",
            "first-rogue",
            "first conflict",
        ).parent
        rogue_agent = self.codex_home / "agents" / "second-rogue.toml"
        rogue_agent.write_text('name = "second-rogue"\n', encoding="utf-8")
        before_skill = self.snapshot_tree(rogue_skill)
        before_agent = rogue_agent.read_bytes()

        self.prepare(
            expect=1,
            HARNESS_TEST_CODEX_QUARANTINE_FAIL_AFTER="1",
        )

        self.assertEqual(self.snapshot_tree(rogue_skill), before_skill)
        self.assertEqual(rogue_agent.read_bytes(), before_agent)
        quarantine = self.codex_home / ".surface-quarantine"
        self.assertFalse(
            any(quarantine.rglob("first-rogue")) if quarantine.exists() else False
        )
        self.assertFalse(
            any(quarantine.rglob("second-rogue.toml"))
            if quarantine.exists()
            else False
        )
        self.assertEqual(self.staging_artifacts(), [])

    def test_outer_terminate_after_first_of_two_quarantines_rolls_back_transaction(self):
        self.prepare()
        managed = self.managed_output_paths()
        managed_before = self.snapshot_paths(managed)
        global_marketplace = (
            self.home / ".codex" / ".tmp" / "bundled-marketplaces" / "openai-bundled"
        )
        global_before = self.snapshot_tree(global_marketplace)
        source_skill = (
            self.no_marketplace
            / "plugins"
            / "computer-use"
            / "skills"
            / "computer-use"
            / "SKILL.md"
        )
        source_skill.write_text(
            source_skill.read_text(encoding="utf-8") + "outer signal global update\n",
            encoding="utf-8",
        )
        alpha = self.repo / ".claude" / "skills" / "alpha" / "SKILL.md"
        alpha.write_text(
            alpha.read_text(encoding="utf-8") + "outer signal local update\n",
            encoding="utf-8",
        )
        rogue_skill = write_skill(
            self.codex_home / "skills",
            "first-rogue",
            "first-rogue",
            "first signal conflict",
        ).parent
        rogue_agent = self.codex_home / "agents" / "second-rogue.toml"
        rogue_agent.write_text('name = "second-rogue"\n', encoding="utf-8")
        rogue_skill_before = self.snapshot_tree(rogue_skill)
        rogue_agent_before = rogue_agent.read_bytes()
        ready = self.tmp / "quarantine-ready"
        release = self.tmp / "quarantine-release"
        process = self.start_prepare(
            HARNESS_TEST_CODEX_QUARANTINE_READY=str(ready),
            HARNESS_TEST_CODEX_QUARANTINE_RELEASE=str(release),
        )
        self.wait_for_condition(process, ready.exists, f"gate {ready}")

        process.terminate()
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            release.touch()
            stdout, stderr = process.communicate(timeout=COORDINATION_TIMEOUT_SECONDS)
            self.fail(
                "outer prepare did not immediately forward SIGTERM to publisher\n"
                f"stdout={stdout}\nstderr={stderr}"
            )

        self.assertNotEqual(process.returncode, 0, f"stdout={stdout}\nstderr={stderr}")
        self.assertEqual(self.snapshot_paths(managed), managed_before)
        self.assertEqual(self.snapshot_tree(global_marketplace), global_before)
        self.assertEqual(self.snapshot_tree(rogue_skill), rogue_skill_before)
        self.assertEqual(rogue_agent.read_bytes(), rogue_agent_before)
        self.assertEqual(self.staging_artifacts(), [])
        self.assertEqual(
            list(global_marketplace.parent.glob(".openai-bundled-candidate.*")), []
        )
        self.assertEqual(
            list(global_marketplace.parent.glob(".openai-bundled-backup.*")), []
        )

    def test_quarantine_destination_race_preserves_concurrent_entry(self):
        self.prepare()
        rogue = write_skill(
            self.codex_home / "skills",
            "racing-rogue",
            "racing-rogue",
            "quarantine destination race",
        ).parent
        rogue_before = self.snapshot_tree(rogue)
        ready = self.tmp / "quarantine-destination-ready"
        release = self.tmp / "quarantine-destination-release"
        process = self.start_prepare(
            HARNESS_TEST_CODEX_QUARANTINE_DESTINATION_READY=str(ready),
            HARNESS_TEST_CODEX_QUARANTINE_DESTINATION_RELEASE=str(release),
        )
        self.wait_for_condition(process, ready.exists, f"gate {ready}")
        competing = (
            self.codex_home / ".surface-quarantine" / "skills" / "racing-rogue"
        )
        competing.mkdir(parents=True)
        (competing / "owner.txt").write_text("concurrent owner\n", encoding="utf-8")
        release.touch()
        stdout, stderr = process.communicate(timeout=COORDINATION_TIMEOUT_SECONDS)

        self.assertEqual(process.returncode, 0, f"stdout={stdout}\nstderr={stderr}")
        self.assertEqual((competing / "owner.txt").read_text(), "concurrent owner\n")
        self.assertEqual(
            self.snapshot_tree(competing.with_name("racing-rogue.1")), rogue_before
        )
        self.assertFalse(rogue.exists())

    def test_hook_commands_shell_quote_special_harness_path(self):
        special = self.tmp / "repo 'quoted' $(not-executed) `still-not-executed`"
        self.repo.rename(special)
        self.repo = special
        self.codex_home = self.repo / ".harness" / "codex"
        self.manifest_path = self.repo / "config" / "codex-surface.json"
        self.mcp_path = self.repo / ".mcp.json"
        (self.repo / ".claude" / "settings.json").write_text(
            json.dumps(
                {
                    "hooks": {
                        "SessionStart": [
                            {
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": 'bash "$CLAUDE_PROJECT_DIR/core/hooks/session-start.sh"',
                                    }
                                ]
                            }
                        ]
                    }
                }
            )
            + "\n",
            encoding="utf-8",
        )

        self.prepare()

        hooks = json.loads((self.codex_home / "hooks.json").read_text(encoding="utf-8"))
        commands = []
        for groups in hooks.get("hooks", {}).values():
            for group in groups:
                for hook in group.get("hooks", []):
                    commands.append(hook["command"])
        self.assertTrue(commands)
        hooks_root = str(self.repo / "core" / "hooks") + os.sep
        for command in commands:
            argv = shlex.split(command)
            if "codex-cmux-title-sync.py" in command:
                self.assertEqual(len(argv), 2, command)
                self.assertEqual(
                    argv[1],
                    str(ROOT / "bin" / "codex-cmux-title-sync.py"),
                    command,
                )
                continue
            self.assertIn(len(argv), (2, 4), command)
            self.assertEqual(argv[0], "bash")
            self.assertTrue(argv[-1].startswith(hooks_root), command)

    def test_warm_path_rejects_conflicting_managed_skill_override(self):
        self.prepare()
        catalog = json.loads((self.codex_home / "skill-catalog.json").read_text(encoding="utf-8"))
        disabled = catalog["disabled_skill_paths"][0]
        with (self.codex_home / "config.toml").open("a", encoding="utf-8") as stream:
            stream.write(
                "\n[[skills.config]]\n"
                f"path = {json.dumps(disabled)}\n"
                "enabled = true\n"
            )
        self.prepare()
        self.assertEqual(self.compiler_calls(), 2)
        config = (self.codex_home / "config.toml").read_text(encoding="utf-8")
        self.assertEqual(config.count(f"path = {json.dumps(disabled)}"), 1)
        block = config.split(f"path = {json.dumps(disabled)}", 1)[1].split("[[", 1)[0]
        self.assertIn("enabled = false", block)

    def test_selected_skill_override_is_removed(self):
        self.prepare()
        catalog = json.loads(
            (self.codex_home / "skill-catalog.json").read_text(encoding="utf-8")
        )
        selected = next(item for item in catalog["skills"] if item["name"] == "alpha")
        with (self.codex_home / "config.toml").open("a", encoding="utf-8") as stream:
            stream.write(
                "\n[[skills.config]]\n"
                f"path = {json.dumps(selected['exposed_path'])}\n"
                "enabled = false\n"
            )

        self.prepare()

        self.assertEqual(self.compiler_calls(), 2)
        config = (self.codex_home / "config.toml").read_text(encoding="utf-8")
        self.assertNotIn(f"path = {json.dumps(selected['exposed_path'])}", config)

    def test_single_quoted_commented_selected_override_is_removed(self):
        self.prepare()
        catalog = json.loads(
            (self.codex_home / "skill-catalog.json").read_text(encoding="utf-8")
        )
        selected = next(item for item in catalog["skills"] if item["name"] == "alpha")
        with (self.codex_home / "config.toml").open("a", encoding="utf-8") as stream:
            stream.write(
                "\n[[skills.config]]\n"
                f"path = '{selected['exposed_path']}'\n"
                "enabled = false # user choice\n"
            )

        self.prepare()

        self.assertEqual(self.compiler_calls(), 2)
        with (self.codex_home / "config.toml").open("rb") as stream:
            config = tomllib.load(stream)
        configured = (config.get("skills") or {}).get("config") or []
        self.assertFalse(
            any(item.get("path") == selected["exposed_path"] for item in configured)
        )

    def test_unowned_generated_skill_is_quarantined_and_next_prepare_is_warm(self):
        self.prepare()
        rogue = write_skill(
            self.codex_home / "skills",
            "rogue-copy",
            "rogue-copy",
            "unowned generated-home drift",
        )
        before = self.snapshot_tree(rogue.parent)

        self.prepare()

        self.assertEqual(self.compiler_calls(), 2)
        self.assertFalse(rogue.parent.exists())
        quarantined = self.codex_home / ".surface-quarantine" / "skills" / "rogue-copy"
        self.assertEqual(self.snapshot_tree(quarantined), before)
        self.prepare()
        self.assertEqual(self.compiler_calls(), 2, "quarantined skill forced perpetual cold rebuilds")

    def test_poisoned_managed_marker_quarantines_unowned_skill(self):
        self.prepare()
        rogue = write_skill(
            self.codex_home / "skills",
            "rogue-marker-bypass",
            "rogue-marker-bypass",
            "unowned marker poisoning",
        )
        marker = self.codex_home / "skills" / ".harness-managed"
        with marker.open("a", encoding="utf-8") as stream:
            stream.write("rogue-marker-bypass\n")

        self.prepare()

        self.assertEqual(self.compiler_calls(), 2)
        self.assertFalse(rogue.parent.exists())
        quarantined = (
            self.codex_home
            / ".surface-quarantine"
            / "skills"
            / "rogue-marker-bypass"
        )
        self.assertTrue((quarantined / "SKILL.md").is_file())
        self.assertNotIn("rogue-marker-bypass", marker.read_text(encoding="utf-8"))

    def test_poisoned_agent_marker_quarantines_unowned_agent_and_next_prepare_is_warm(self):
        self.prepare()
        agents = self.codex_home / "agents"
        rogue = agents / "rogue.toml"
        rogue.write_text('name = "rogue"\n', encoding="utf-8")
        before = rogue.read_bytes()
        marker = agents / ".harness-managed"
        with marker.open("a", encoding="utf-8") as stream:
            stream.write("rogue.toml\n")

        self.prepare()

        self.assertEqual(self.compiler_calls(), 2)
        self.assertFalse(rogue.exists())
        quarantined = (
            self.codex_home / ".surface-quarantine" / "agents" / "rogue.toml"
        )
        self.assertEqual(quarantined.read_bytes(), before)
        self.assertNotIn("rogue.toml", marker.read_text(encoding="utf-8"))
        self.prepare()
        self.assertEqual(self.compiler_calls(), 2, "quarantined agent forced perpetual cold rebuilds")

    def allow_external_agents(self):
        manifest = json.loads(self.manifest_path.read_text())
        manifest["agents"] = {"external_filename_prefixes": ["glider-"]}
        self.manifest_path.write_text(json.dumps(manifest))

    def test_external_agents_survive_warm_and_cold_prepare_without_becoming_managed(self):
        self.allow_external_agents()
        self.prepare()
        agent = self.codex_home / "agents" / "glider-example.toml"
        agent.write_text('name = "glider-example"\ndeveloper_instructions = "plugin owned"\n')
        before = agent.read_bytes()
        self.prepare()
        self.assertEqual(self.compiler_calls(), 1, "external sync must not force a rebuild")
        self.assertEqual(agent.read_bytes(), before)
        (self.repo / ".claude/skills/alpha/SKILL.md").write_text(
            "---\nname: alpha\ndescription: changed\n---\nchanged\n"
        )
        self.prepare()
        self.assertEqual(self.compiler_calls(), 2)
        self.assertEqual(agent.read_bytes(), before)
        self.assertNotIn(agent.name, (self.codex_home / "agents/.harness-managed").read_text())
        stamp = json.loads((self.codex_home / ".surface-success.json").read_text())
        self.assertNotIn("agents/" + agent.name, stamp["output_signatures"])
        self.prepare()
        self.assertEqual(self.compiler_calls(), 2)

    def test_external_agent_allowlist_does_not_preserve_other_files_or_symlinks(self):
        self.allow_external_agents()
        self.prepare()
        agents = self.codex_home / "agents"
        (agents / "unknown.toml").write_text('name = "unknown"\n')
        target = self.tmp / "target.toml"
        target.write_text('name = "external-target"\n')
        (agents / "glider-link.toml").symlink_to(target)
        self.prepare()
        self.assertFalse((agents / "unknown.toml").exists())
        self.assertFalse((agents / "glider-link.toml").is_symlink())
        self.assertEqual(target.read_text(), 'name = "external-target"\n')
        quarantine = self.codex_home / ".surface-quarantine/agents"
        self.assertTrue((quarantine / "unknown.toml").is_file())
        self.assertTrue((quarantine / "glider-link.toml").is_symlink())

    def test_external_agent_broken_and_directory_symlinks_force_quarantine(self):
        self.allow_external_agents()
        self.prepare()
        agents = self.codex_home / "agents"
        broken = agents / "glider-broken.toml"
        directory_link = agents / "glider-directory.toml"
        broken.symlink_to(self.tmp / "missing-agent")
        directory_link.symlink_to(self.home, target_is_directory=True)
        self.prepare()
        self.assertEqual(self.compiler_calls(), 2)
        self.assertFalse(broken.is_symlink())
        self.assertFalse(directory_link.is_symlink())
        quarantine = self.codex_home / ".surface-quarantine/agents"
        self.assertTrue((quarantine / broken.name).is_symlink())
        self.assertTrue((quarantine / directory_link.name).is_symlink())

    def test_external_claude_agents_are_not_converted_over_native_definitions(self):
        self.allow_external_agents()
        self.prepare()
        agent = self.codex_home / "agents/glider-example.toml"
        agent.write_text('name = "glider-example"\n')
        before = agent.read_bytes()
        source = self.repo / ".claude/agents/glider-example.md"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text('---\nname: glider-example\ndescription: external Claude agent\n---\nprovider body\n')
        self.prepare()
        self.assertEqual(agent.read_bytes(), before)
        self.assertNotIn(agent.name, (self.codex_home / "agents/.harness-managed").read_text())
        agent.unlink()
        source.write_text(source.read_text() + "new provider revision\n")
        self.prepare()
        self.assertFalse(agent.exists(), "Claude provider file must not become a lossy native agent")

    def test_external_agent_prefixes_reject_empty_or_path_patterns(self):
        for prefix in ("", "../", "*", "glider/"):
            with self.subTest(prefix=prefix):
                manifest = json.loads(self.manifest_path.read_text())
                manifest["agents"] = {"external_filename_prefixes": [prefix]}
                self.manifest_path.write_text(json.dumps(manifest))
                result = self.prepare(expect=2)
                self.assertIn("external_filename_prefixes", result.stderr)
                self.assertFalse(self.counter.exists())

    def test_product_plugin_skill_drift_forces_rebuild(self):
        self.prepare(HARNESS_CODEX_MCP_PROFILE="work")
        catalog = json.loads(
            (self.codex_home / "skill-catalog.json").read_text(encoding="utf-8")
        )
        computer = next(
            item
            for item in catalog["skills"]
            if item["name"] == "computer-use:computer-use"
        )
        skill_path = Path(computer["source_path"])
        skill_path.write_text(
            skill_path.read_text(encoding="utf-8").replace(
                "name: computer-use", "name: computer-use-mutated", 1
            ),
            encoding="utf-8",
        )

        self.prepare(HARNESS_CODEX_MCP_PROFILE="work")

        self.assertEqual(self.compiler_calls(), 2)
        repaired = skill_path.read_text(encoding="utf-8")
        self.assertIn("name: computer-use\n", repaired)
        self.assertNotIn("computer-use-mutated", repaired)

    def test_warm_path_rejects_rogue_mcp_table(self):
        self.prepare()
        with (self.codex_home / "config.toml").open("a", encoding="utf-8") as stream:
            stream.write(
                "\n[mcp_servers.rogue]\n"
                'command = "rogue"\n'
                "enabled = true\n"
            )

        self.prepare()

        self.assertEqual(self.compiler_calls(), 2)
        config = (self.codex_home / "config.toml").read_text(encoding="utf-8")
        self.assertNotIn("[mcp_servers.rogue]", config)

    def test_warm_path_rejects_semantic_mcp_table_bypasses(self):
        for suffix in (
            '\n[mcp_servers]\nrogue_inline = { command = "printf", args = ["rogue"], enabled = true }\n',
            '\n[mcp_servers."rogue.dotted"]\ncommand = "printf"\nenabled = true\n',
        ):
            with self.subTest(suffix=suffix.splitlines()[1]):
                self.prepare()
                before = self.compiler_calls()
                with (self.codex_home / "config.toml").open("a", encoding="utf-8") as stream:
                    stream.write(suffix)

                self.prepare()

                self.assertEqual(self.compiler_calls(), before + 1)
                with (self.codex_home / "config.toml").open("rb") as stream:
                    config = tomllib.load(stream)
                self.assertEqual(
                    set(config.get("mcp_servers") or {}),
                    {"context7", "harness-rag", "jira"},
                )

    def test_warm_path_repairs_launcher_owned_output_drift(self):
        self.prepare()
        agents = self.codex_home / "AGENTS.md"
        with agents.open("a", encoding="utf-8") as stream:
            stream.write("ROGUE_ALWAYS_ON_SENTINEL\n")
        hooks = self.codex_home / "hooks.json"
        hooks.write_text(
            json.dumps(
                {
                    "hooks": {
                        "SessionStart": [
                            {"hooks": [{"type": "command", "command": "rogue"}]}
                        ]
                    }
                }
            )
            + "\n",
            encoding="utf-8",
        )

        self.prepare()

        self.assertEqual(self.compiler_calls(), 2)
        self.assertNotIn("ROGUE_ALWAYS_ON_SENTINEL", agents.read_text(encoding="utf-8"))
        self.assertNotIn("rogue", hooks.read_text(encoding="utf-8"))

    def test_hook_entrypoint_removal_invalidates_warm_surface(self):
        self.prepare()
        hook = self.repo / "core" / "hooks" / "session-start.sh"
        hook.unlink()

        self.prepare()

        self.assertEqual(self.compiler_calls(), 2)
        hooks = json.loads((self.codex_home / "hooks.json").read_text(encoding="utf-8"))
        commands = [
            item["command"]
            for groups in (hooks.get("hooks") or {}).values()
            for group in groups
            for item in group.get("hooks", [])
        ]
        self.assertFalse(any("session-start.sh" in command for command in commands))

    def test_warm_path_repairs_launcher_owned_config_semantics(self):
        self.prepare()
        config_path = self.codex_home / "config.toml"
        config = config_path.read_text(encoding="utf-8")
        config = config.replace('model = "gpt-5.6-terra"', 'model = "rogue-model"', 1)
        config = config.replace("apps = false", "apps = true", 1)
        config = config.replace("hooks = true", "hooks = false", 1)
        config = config.replace("experimental_mode = true", "experimental_mode = false", 1)
        config = config.replace(
            "[tools.update_plan]\nenabled = true",
            "[tools.update_plan]\nenabled = false",
            1,
        )
        config = config.replace(
            'model_reasoning_effort = "medium"',
            'model_reasoning_effort = "medium"\n'
            'approval_policy = "never"\n'
            'sandbox_mode = "danger-full-access"',
            1,
        )
        # Explicit-mode key mutation is covered separately; auto omits both.
        config = config.replace(
            "model_context_window = 272000", "model_context_window = 123456", 1
        )
        config = config.replace(
            "model_auto_compact_token_limit = 217600",
            "model_auto_compact_token_limit = 900000",
            1,
        )
        config = config.replace(
            'command = "context7"', 'command = "/tmp/rogue-mcp"', 1
        )
        config_path.write_text(config, encoding="utf-8")

        self.prepare()

        self.assertEqual(self.compiler_calls(), 2)
        with config_path.open("rb") as stream:
            repaired = tomllib.load(stream)
        self.assertEqual(repaired["model"], "gpt-5.6-terra")
        self.assertNotIn("approval_policy", repaired)
        self.assertNotIn("sandbox_mode", repaired)
        # Auto repair leaves both keys absent.
        self.assertNotIn("model_context_window", repaired)
        self.assertNotIn("model_auto_compact_token_limit", repaired)
        self.assertEqual(repaired["tools"], {"update_plan": {"enabled": True}})
        self.assertEqual(repaired["mcp_servers"]["context7"]["command"], "context7")
        self.assertEqual(
            repaired["features"],
            {
                "apps": False,
                "goals": True,
                "hooks": True,
                "multi_agent": True,
                "context_management": {"experimental_mode": True},
            },
        )

        self.prepare()
        self.assertEqual(
            self.compiler_calls(),
            2,
            "repaired context-management config forced perpetual cold rebuilds",
        )

    def assert_native_task_tracker_repaired(self, replacement: str):
        self.prepare()
        config_path = self.codex_home / "config.toml"
        config = config_path.read_text(encoding="utf-8")
        original = "[tools.update_plan]\nenabled = true"
        self.assertIn(original, config)
        config_path.write_text(config.replace(original, replacement, 1), encoding="utf-8")

        catalog = json.loads((self.codex_home / "skill-catalog.json").read_text())
        warm_environment = {
            "HOME": str(self.home),
            "HARNESS_CODEX_APPS_ALLOWLIST": "",
            "HARNESS_CODEX_CONTEXT_MANAGEMENT_SUPPORTED": "true",
        }
        with mock.patch.dict(os.environ, warm_environment):
            self.assertFalse(
                WARM_PROBE_MODULE.config_matches(
                    self.codex_home, catalog, "", None, None
                )
            )

        self.prepare()
        with config_path.open("rb") as stream:
            repaired = tomllib.load(stream)
        self.assertEqual(repaired["tools"], {"update_plan": {"enabled": True}})

        with mock.patch.dict(os.environ, warm_environment):
            self.assertTrue(
                WARM_PROBE_MODULE.config_matches(
                    self.codex_home, catalog, "", None, None
                )
            )
        self.prepare()
        self.assertEqual(self.compiler_calls(), 2)

    def test_warm_path_repairs_missing_native_task_tracker(self):
        self.assert_native_task_tracker_repaired("")

    def test_warm_path_repairs_disabled_native_task_tracker(self):
        self.assert_native_task_tracker_repaired("[tools.update_plan]\nenabled = false")

    def test_context_management_capability_change_rebuilds_once_then_stays_warm(self):
        self.prepare()
        self.assertEqual(self.compiler_calls(), 1)
        with (self.codex_home / "config.toml").open("rb") as stream:
            supported = tomllib.load(stream)
        self.assertEqual(
            supported["features"]["context_management"],
            {"experimental_mode": True},
        )

        self.codex_bin.write_text(
            '#!/usr/bin/env bash\necho "codex-cli 0.152.1"\n',
            encoding="utf-8",
        )
        self.prepare()
        self.assertEqual(self.compiler_calls(), 2)
        with (self.codex_home / "config.toml").open("rb") as stream:
            unsupported = tomllib.load(stream)
        self.assertNotIn("context_management", unsupported["features"])
        self.prepare()
        self.assertEqual(self.compiler_calls(), 2)

        self.codex_bin.write_text(
            '#!/usr/bin/env bash\necho "codex-cli 0.153.0"\n',
            encoding="utf-8",
        )
        self.prepare()
        self.assertEqual(self.compiler_calls(), 3)
        with (self.codex_home / "config.toml").open("rb") as stream:
            restored = tomllib.load(stream)
        self.assertEqual(
            restored["features"]["context_management"],
            {"experimental_mode": True},
        )
        self.prepare()
        self.assertEqual(self.compiler_calls(), 3)

    def test_warm_path_rebuilds_missing_explicit_policy(self):
        self.prepare()
        catalog = json.loads(
            (self.codex_home / "skill-catalog.json").read_text(encoding="utf-8")
        )
        explicit = next(
            item for item in catalog["skills"] if item["name"] == "project-explicit"
        )
        policy = Path(explicit["exposed_path"]).parent / "agents" / "openai.yaml"
        policy.unlink()

        self.prepare()

        self.assertEqual(self.compiler_calls(), 2)
        self.assertIn("allow_implicit_invocation: false", policy.read_text())

    def test_new_unapproved_plugin_invalidates_and_is_disabled(self):
        self.prepare()
        self.assertEqual(self.compiler_calls(), 1)
        unapproved = write_skill(
            self.home
            / ".claude"
            / "plugins"
            / "cache"
            / "extra-marketplace"
            / "extra-plugin"
            / "1.0.0"
            / "skills",
            "surprise",
            "surprise",
            "new unapproved plugin",
        )
        self.prepare()
        self.assertEqual(self.compiler_calls(), 2)
        catalog = json.loads(
            (self.codex_home / "skill-catalog.json").read_text(encoding="utf-8")
        )
        self.assertNotIn(
            "extra-plugin:surprise", {item["name"] for item in catalog["skills"]}
        )
        self.assertIn(str(unapproved.resolve()), catalog["disabled_skill_paths"])
        config = (self.codex_home / "config.toml").read_text(encoding="utf-8")
        self.assertIn(f"path = {json.dumps(str(unapproved.resolve()))}", config)

    def test_new_unlisted_codex_only_skill_invalidates_empty_default_profile(self):
        self.prepare()
        self.assertEqual(self.compiler_calls(), 1)
        unlisted = write_skill(
            self.repo / ".codex-only" / "repos" / "taste-skill" / "skills",
            "surprise-design",
            "surprise-design",
            "new unlisted design skill",
        )

        self.prepare()

        self.assertEqual(self.compiler_calls(), 2)
        catalog = json.loads(
            (self.codex_home / "skill-catalog.json").read_text(encoding="utf-8")
        )
        self.assertNotIn(
            "surprise-design", {item["name"] for item in catalog["skills"]}
        )
        self.assertIn(str(unlisted.resolve()), catalog["disabled_skill_paths"])

    def test_symlinked_skill_store_retarget_invalidates(self):
        first = self.tmp / "global-store-a"
        second = self.tmp / "global-store-b"
        first_skill = write_skill(first, "first", "first", "first global route")
        second_skill = write_skill(second, "second", "second", "second global route")
        global_parent = self.home / ".agents"
        global_parent.mkdir(parents=True)
        global_root = global_parent / "skills"
        global_root.symlink_to(first, target_is_directory=True)

        self.prepare()
        self.assertEqual(self.compiler_calls(), 1)
        global_root.unlink()
        global_root.symlink_to(second, target_is_directory=True)
        self.prepare()

        self.assertEqual(self.compiler_calls(), 2)
        catalog = json.loads(
            (self.codex_home / "skill-catalog.json").read_text(encoding="utf-8")
        )
        self.assertNotIn(str(first_skill.resolve()), catalog["disabled_skill_paths"])
        self.assertIn(str(second_skill.resolve()), catalog["disabled_skill_paths"])

    def test_manifest_skill_wins_command_collision_without_source_write(self):
        source_skill = write_skill(
            self.repo / ".claude" / "skills",
            "game-dev-loop",
            "game-dev-loop",
            "canonical project skill",
            implicit=True,
        )
        command_dir = self.repo / ".claude" / "commands"
        command_dir.mkdir()
        (command_dir / "game-dev-loop.md").write_text(
            "---\ndescription: generated command collision\n---\n\n# Command body\n",
            encoding="utf-8",
        )
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        manifest["skills"]["project"]["implicit"].append("game-dev-loop")
        manifest["skills"]["commands"]["explicit_only"].append("game-dev-loop")
        self.manifest_path.write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )

        skills_dir = self.codex_home / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "game-dev-loop").symlink_to(
            source_skill.parent, target_is_directory=True
        )
        (skills_dir / ".harness-managed").write_text(
            "game-dev-loop\n", encoding="utf-8"
        )
        (skills_dir / ".harness-managed-cmds").write_text(
            "game-dev-loop\n", encoding="utf-8"
        )
        skill_before = source_skill.read_bytes()
        policy = source_skill.parent / "agents" / "openai.yaml"
        policy_before = policy.read_bytes()

        self.prepare()

        self.assertEqual(source_skill.read_bytes(), skill_before)
        self.assertEqual(policy.read_bytes(), policy_before)
        self.assertIn("allow_implicit_invocation: true", policy.read_text())
        catalog = json.loads(
            (self.codex_home / "skill-catalog.json").read_text(encoding="utf-8")
        )
        entry = next(item for item in catalog["skills"] if item["name"] == "game-dev-loop")
        self.assertEqual(entry["invocation"], "implicit")
        exposed = Path(entry["exposed_path"])
        self.assertTrue(exposed.parent.is_symlink())
        self.assertEqual(exposed.resolve(), source_skill.resolve())
        self.assertFalse((skills_dir / ".harness-managed-cmds").exists())

    def test_manifest_explicit_command_remains_callable_without_prompt_exposure(self):
        command_dir = self.repo / ".claude" / "commands"
        command_dir.mkdir()
        command = command_dir / "daily-pipeline.md"
        command.write_text(
            "# Daily pipeline\n\nRun the daily pipeline in sequence.\n",
            encoding="utf-8",
        )
        before = command.read_bytes()
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        manifest["skills"]["commands"]["explicit_only"].append("daily-pipeline")
        self.manifest_path.write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )

        # Simulate a home prepared by the legacy command generator. Surface
        # mode must migrate this real directory before claiming the exact
        # command destination for its explicit-only wrapper.
        skills_dir = self.codex_home / "skills"
        legacy = skills_dir / "daily-pipeline"
        (legacy / "agents").mkdir(parents=True)
        (legacy / "SKILL.md").write_text(
            "---\n"
            "name: daily-pipeline\n"
            "description: Generated by codex-home-prepare.sh from "
            ".claude/commands/daily-pipeline.md\n"
            "---\n",
            encoding="utf-8",
        )
        (legacy / "agents" / "openai.yaml").write_text(
            "policy:\n  allow_implicit_invocation: false\n", encoding="utf-8"
        )
        (skills_dir / ".harness-managed-cmds").write_text(
            "daily-pipeline\n", encoding="utf-8"
        )

        self.prepare()

        self.assertEqual(command.read_bytes(), before)
        catalog = json.loads(
            (self.codex_home / "skill-catalog.json").read_text(encoding="utf-8")
        )
        entry = next(item for item in catalog["skills"] if item["name"] == "daily-pipeline")
        self.assertEqual(entry["source"], "repo-command")
        self.assertEqual(entry["invocation"], "explicit_only")
        exposed = Path(entry["exposed_path"])
        self.assertTrue((exposed.parent / ".harness-surface-wrapper").exists())
        self.assertFalse((skills_dir / ".harness-managed-cmds").exists())
        self.assertIn("name: daily-pipeline", exposed.read_text())
        self.assertIn(
            'description: "Run the daily pipeline in sequence."', exposed.read_text()
        )
        self.assertIn(
            "allow_implicit_invocation: false",
            (exposed.parent / "agents" / "openai.yaml").read_text(),
        )

    def test_legacy_command_marker_cannot_escape_skills_directory(self):
        skills_dir = self.codex_home / "skills"
        skills_dir.mkdir(parents=True)
        outside = self.codex_home / "outside"
        outside.mkdir()
        (outside / "SKILL.md").write_text(
            "---\n"
            "name: outside\n"
            "description: Generated by codex-home-prepare.sh from "
            ".claude/commands/outside.md\n"
            "---\n",
            encoding="utf-8",
        )
        (skills_dir / ".harness-managed-cmds").write_text(
            "../outside\n", encoding="utf-8"
        )

        self.prepare()

        self.assertTrue(outside.is_dir())
        self.assertTrue((outside / "SKILL.md").is_file())


class RuntimeHooksOptinTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="runtime-hooks-optin."))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        sys.path.insert(0, str(ROOT / "bin"))
        try:
            import runtime_hooks_optin
            import orca_hooks_optin
        finally:
            sys.path.pop(0)
        self.runtime = runtime_hooks_optin
        self.orca = orca_hooks_optin

    def launcher_env(self, *lines):
        (self.tmp / "config").mkdir(exist_ok=True)
        (self.tmp / "config" / "launcher.env").write_text(
            "".join(line + "\n" for line in lines), encoding="utf-8"
        )

    def cli(self, script, *args, env=None):
        return subprocess.run(
            [sys.executable, str(ROOT / "bin" / script), *args],
            capture_output=True, text=True,
            env={"PATH": os.environ.get("PATH", ""), **(env or {})},
        )

    def test_resolve_returns_exactly_the_registry_keys(self):
        self.assertEqual(self.runtime.resolve(str(self.tmp)), {"orca": False, "herdr": False, "launch_record": False, "ssot": False})
        self.launcher_env("HARNESS_ORCA_AGENT_HOOKS=1")
        self.assertEqual(self.runtime.resolve(str(self.tmp)), {"orca": True, "herdr": False, "launch_record": False, "ssot": False})
        self.launcher_env("HARNESS_HERDR_AGENT_HOOKS=1")
        self.assertEqual(self.runtime.resolve(str(self.tmp)), {"orca": False, "herdr": True, "launch_record": False, "ssot": False})
        self.launcher_env("HARNESS_ORCA_AGENT_HOOKS=1", "HARNESS_HERDR_AGENT_HOOKS=1")
        self.assertEqual(self.runtime.resolve(str(self.tmp)), {"orca": True, "herdr": True, "launch_record": False, "ssot": False})
        self.launcher_env("HARNESS_LAUNCH_RECORD_HOOKS=1")
        self.assertEqual(self.runtime.resolve(str(self.tmp)), {"orca": False, "herdr": False, "launch_record": True, "ssot": False})

    def test_ssot_optin_requires_strict_profile_json(self):
        self.launcher_env('HARNESS_SSOT_SESSION_HOOKS=1')
        self.assertFalse(self.runtime.resolve(str(self.tmp))['ssot'])
        path = self.tmp / 'config' / 'ssot-session-hooks.json'
        path.write_text('{"enabled":true}')
        self.assertTrue(self.runtime.resolve(str(self.tmp))['ssot'])
        for invalid in ('{"enabled":"true"}', '{"enabled":1}', '{"enabled":false}',
                        '{"enabled":true,"extra":1}', 'broken', '{"enabled":true}' + ' ' * 4097 + 'junk'):
            path.write_text(invalid)
            self.assertFalse(self.runtime.resolve(str(self.tmp))['ssot'])

    def test_all_keys_share_the_l3_parsing_rule_and_ignore_the_environment(self):
        with mock.patch.dict(os.environ, {"HARNESS_ORCA_AGENT_HOOKS": "1", "HARNESS_HERDR_AGENT_HOOKS": "1",
                                          "HARNESS_LAUNCH_RECORD_HOOKS": "1"}):
            self.assertEqual(self.runtime.resolve(str(self.tmp)), {"orca": False, "herdr": False, "launch_record": False, "ssot": False})
        for key, name in (("HARNESS_ORCA_AGENT_HOOKS", "orca"), ("HARNESS_HERDR_AGENT_HOOKS", "herdr"),
                          ("HARNESS_LAUNCH_RECORD_HOOKS", "launch_record")):
            for lines, expected in (
                ([f"{key}=1"], True),
                ([f'{key}="1"'], True),
                ([f"export {key}='1'"], True),
                ([f"  {key}=1  "], True),
                ([f"{key}=1", f'{key}="0"'], False),
                ([f"{key}=0", f"{key}=1"], True),
                ([f"{key}=true"], False),
                ([f"{key}=yes"], False),
                ([f"{key}="], False),
                ([f'{key}="1\''], False),
                ([f"#{key}=1"], False),
                ([f"X{key}=1"], False),
                ([f"{key}_EXTRA=1"], False),
            ):
                with self.subTest(key=key, lines=lines):
                    self.launcher_env(*lines)
                    result = self.runtime.resolve(str(self.tmp))
                    self.assertEqual(result[name], expected)
                    self.assertEqual([other for other in result if result[other] and other != name], [])

    def test_unreadable_launcher_env_resolves_off(self):
        (self.tmp / "config").mkdir()
        (self.tmp / "config" / "launcher.env").write_bytes(b"\xff\xfeHARNESS_HERDR_AGENT_HOOKS=1\n")
        self.assertEqual(self.runtime.resolve(str(self.tmp)), {"orca": False, "herdr": False, "launch_record": False, "ssot": False})
        self.assertEqual(self.runtime.resolve(str(self.tmp / "missing")), {"orca": False, "herdr": False, "launch_record": False, "ssot": False})

    def test_cli_prints_one_stable_line(self):
        self.assertEqual(self.cli("runtime_hooks_optin.py", str(self.tmp)).stdout, "orca=0 herdr=0 launch_record=0 ssot=0\n")
        self.launcher_env("HARNESS_HERDR_AGENT_HOOKS=1")
        self.assertEqual(self.cli("runtime_hooks_optin.py", str(self.tmp)).stdout, "orca=0 herdr=1 launch_record=0 ssot=0\n")
        self.launcher_env("HARNESS_ORCA_AGENT_HOOKS=1")
        self.assertEqual(self.cli("runtime_hooks_optin.py", str(self.tmp)).stdout, "orca=1 herdr=0 launch_record=0 ssot=0\n")
        self.launcher_env("HARNESS_ORCA_AGENT_HOOKS=1", "HARNESS_HERDR_AGENT_HOOKS=1")
        result = self.cli("runtime_hooks_optin.py", str(self.tmp),
                          env={"HARNESS_ORCA_AGENT_HOOKS": "0", "HARNESS_HERDR_AGENT_HOOKS": "0"})
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "orca=1 herdr=1 launch_record=0 ssot=0\n", ""))
        usage = self.cli("runtime_hooks_optin.py")
        self.assertNotEqual(usage.returncode, 0)
        self.assertEqual(usage.stdout, "")

    def test_orca_wrapper_keeps_the_v0_34_0_interface(self):
        self.assertEqual(self.orca.KEY, "HARNESS_ORCA_AGENT_HOOKS")
        self.assertEqual(self.orca.resolve(str(self.tmp)), "")
        self.launcher_env("HARNESS_HERDR_AGENT_HOOKS=1")
        self.assertEqual(self.orca.resolve(str(self.tmp)), "", "herdr must not enable the Orca wrapper")
        self.launcher_env("HARNESS_ORCA_AGENT_HOOKS=1")
        self.assertEqual(self.orca.resolve(str(self.tmp)), "1")
        # The CLI writes the bare value with no newline, exactly as before.
        self.assertEqual(self.cli("orca_hooks_optin.py", str(self.tmp)).stdout, "1")
        self.launcher_env('HARNESS_ORCA_AGENT_HOOKS="0"')
        self.assertEqual(self.cli("orca_hooks_optin.py", str(self.tmp)).stdout, "")
        usage = self.cli("orca_hooks_optin.py")
        self.assertNotEqual(usage.returncode, 0)
        self.assertIn("usage: orca_hooks_optin.py HARNESS_DIR", usage.stderr)


class SurfaceInspectionTests(unittest.TestCase):
    def test_profile_overlay_is_read_only_and_omits_secrets(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            (home / "config.toml").write_text('model = "gpt-5.6-terra"\nmodel_reasoning_effort = "medium"\nmodel_context_window = 1000000\n[model_providers.private]\nbase_url = "https://SECRET_ENDPOINT"\nenv_key = "SECRET_KEY"\n')
            (home / "astra.config.toml").write_text('model = "gpt-6-astra"\nmodel_reasoning_effort = "high"\n')
            before = {p.name: p.read_bytes() for p in home.iterdir()}
            result = subprocess.run([sys.executable, str(RESOLVER), "inspect", "--codex-home", td, "--profile", "astra"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)
            self.assertEqual(data["configured"]["model"], {"value": "gpt-6-astra", "source": "astra.config.toml"})
            self.assertEqual(data["configured"]["model_context_window"], {"value": 1000000, "source": "config.toml"})
            self.assertEqual(data["observation"], "generated_config_only")
            self.assertIsNone(data["runtime_loaded_model"])
            self.assertEqual(data["preparation"]["output_consistency"], "unknown")
            self.assertNotIn("SECRET", result.stdout)
            self.assertEqual(before, {p.name: p.read_bytes() for p in home.iterdir()})

    def test_stamp_consistency_detects_changed_and_deleted_outputs(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            for name in ("AGENTS.md", "hooks.json", "skill-catalog.json", "surface.config.toml",
                         "fast.config.toml", "base.config.toml", "sol.config.toml", "astra.config.toml",
                         "plan.config.toml", "rich.config.toml", "skills/.harness-managed"):
                path = home / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("")
            (home / "config.toml").write_text('model = "gpt-5.6-terra"\n')
            fingerprint = {"schema_version": 1, "digest": "fixture", "skill_profile": "default",
                           "mcp_profile": "default", "global_mcp_digest": "fixture", "apps_allowlist": "", "bundled_marketplace_path": "fixture"}
            subprocess.run([sys.executable, str(RESOLVER), "write-stamp", "--codex-home", td,
                            "--stamp", str(home / ".surface-success.json"), "--fingerprint-json", json.dumps(fingerprint)], check=True, capture_output=True)
            def inspect():
                result = subprocess.run([sys.executable, str(RESOLVER), "inspect", "--codex-home", td], check=True, capture_output=True, text=True)
                return json.loads(result.stdout)["preparation"]["output_consistency"]
            self.assertEqual(inspect(), "matching")
            (home / "AGENTS.md").write_text("drift")
            self.assertEqual(inspect(), "changed")
            (home / "AGENTS.md").write_text("")
            (home / "rich.config.toml").unlink()
            self.assertEqual(inspect(), "changed")

    def test_inspection_distinguishes_metadata_trust_and_managed_settings(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            for name in ("AGENTS.md", "hooks.json", "skill-catalog.json", "surface.config.toml",
                         "fast.config.toml", "base.config.toml", "sol.config.toml", "astra.config.toml",
                         "plan.config.toml", "rich.config.toml", "skills/.harness-managed"):
                path = home / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("")
            (home / "config.toml").write_text('model = "gpt-5.6-terra"\n')
            profile = home / "astra.config.toml"
            baseline = 'model = "gpt-6-astra"\nmodel_reasoning_effort = "medium"\n'
            profile.write_text(baseline)
            fingerprint = {"schema_version": 1, "digest": "fixture", "skill_profile": "default",
                           "mcp_profile": "full", "global_mcp_digest": "fixture", "apps_allowlist": "", "bundled_marketplace_path": "fixture"}
            subprocess.run([sys.executable, str(RESOLVER), "write-stamp", "--codex-home", td,
                            "--stamp", str(home / ".surface-success.json"), "--fingerprint-json", json.dumps(fingerprint)],
                           check=True, capture_output=True)

            def inspect():
                before = {str(p.relative_to(home)): p.read_bytes() for p in home.rglob("*") if p.is_file()}
                result = subprocess.run([sys.executable, str(RESOLVER), "inspect", "--codex-home", td,
                                         "--profile", "astra"], check=True, capture_output=True, text=True)
                self.assertEqual(before, {str(p.relative_to(home)): p.read_bytes() for p in home.rglob("*") if p.is_file()})
                self.assertNotIn("PRIVATE_PROJECT", result.stdout)
                return json.loads(result.stdout)["preparation"]

            initial = inspect()
            self.assertEqual(initial.get("managed_settings_consistency"), "matching")
            self.assertEqual(initial["trust_settings_consistency"], "matching")
            self.assertFalse(initial["runtime_metadata_changed"])
            profile.write_text(baseline + '\n[tui.model_availability_nux]\ngpt-6-astra = 1\n')
            metadata = inspect()
            self.assertEqual(metadata["output_consistency"], "changed")
            self.assertEqual(metadata["managed_settings_consistency"], "matching")
            self.assertEqual(metadata["trust_settings_consistency"], "matching")
            self.assertTrue(metadata["runtime_metadata_changed"])
            profile.write_text(baseline + '\n[projects."PRIVATE_PROJECT"]\ntrust_level = "trusted"\n')
            trust = inspect()
            self.assertEqual(trust["output_consistency"], "changed")
            self.assertEqual(trust["managed_settings_consistency"], "matching")
            self.assertEqual(trust["trust_settings_consistency"], "changed")
            self.assertFalse(trust["runtime_metadata_changed"])
            profile.write_text(baseline.replace('"medium"', '"high"'))
            self.assertEqual(inspect()["managed_settings_consistency"], "changed")
            profile.write_text(baseline + 'approval_policy = "never"\n')
            self.assertEqual(inspect()["managed_settings_consistency"], "changed")
            profile.write_text(baseline + '\n[tui]\nunknown_future_setting = true\n')
            self.assertEqual(inspect()["managed_settings_consistency"], "changed")
            stamp = json.loads((home / ".surface-success.json").read_text())
            stamp.pop("inspection_config_signatures", None)
            (home / ".surface-success.json").write_text(json.dumps(stamp))
            legacy = inspect()
            self.assertEqual(legacy["managed_settings_consistency"], "unknown")
            self.assertEqual(legacy["trust_settings_consistency"], "unknown")
            self.assertIsNone(legacy["runtime_metadata_changed"])

    def test_missing_profile_never_falls_back_and_malformed_config_is_redacted(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            (home / "config.toml").write_text('model = "gpt-5.6-terra"\n')
            for profile in ["astra", "../outside"]:
                result = subprocess.run([sys.executable, str(RESOLVER), "inspect", "--codex-home", td, "--profile", profile], capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
            (home / "config.toml").write_text('SECRET_INVALID_CONTENT')
            result = subprocess.run([sys.executable, str(RESOLVER), "inspect", "--codex-home", td], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("SECRET", result.stderr)


if __name__ == "__main__":
    unittest.main(testRunner=runner_from_env())
