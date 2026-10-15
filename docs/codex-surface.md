# Codex surface manifests

Projects with many skills or MCP servers can opt into an exact runtime surface by adding `config/codex-surface.json`. The launcher validates schema version `1`, expands host paths locally, and generates the project-scoped Codex home. Projects without a manifest keep the legacy merge behavior.

`codex-global-allowlist` is an optional MCP definition source. It never imports all of `$HOME/.codex/config.toml`: launcher-owned native entrypoints forward only the normalized nonempty `HARNESS_CODEX_GLOBAL_MCP_ALLOWLIST` loaded from the project's trusted `config/launcher.env`. The global TOML is authoritative for definition fields, while this manifest's exact profiles remain authoritative for enablement.

## Contract

Every membership list is an allowlist and every `unlisted` policy must be `"disabled"`. Portable paths may use `${HOME}`, `${REPO_ROOT}`, and `${CODEX_HOME}`; committed absolute home paths are unnecessary.

The optional top-level `agents.external_filename_prefixes` list reserves
provider-owned filename namespaces, for example `{"agents":{"external_filename_prefixes":["glider-"]}}`.
The default is empty. Prefixes must be unique portable names ending in `-`, with
no paths or glob syntax. Matching Claude `.md` files are excluded from conversion
because providers may carry client-specific MCP bindings. Matching regular,
non-symlink native `.toml` files remain externally owned: they are never signed,
published, or listed in `.harness-managed`. Other files retain quarantine policy.
An attempted publication collision fails before any live file is moved. Changes
to project Claude agent files still invalidate the cold fingerprint once; stable
provider sync and native agent updates do not force recurring rebuilds.

Install native Codex plugins through the native CLI in the target `CODEX_HOME`.
Their config and cache remain runtime-owned; do not also select their Claude
plugin skills into the generated home. The exact launcher skill catalog covers
launcher-selected routes; native plugin inventory is verified separately.
An optional `runtime_audit.native_codex_plugins` list of `<plugin>@<marketplace>`
ids names plugins the harness requires natively. The launcher does not validate
the rest of `runtime_audit`; it reads this list only to carry those plugins from
the canonical home into isolated session homes, as described in
[Native plugins in isolated sessions](codex-integration.md#native-plugins-in-isolated-sessions).

```json
{
  "schema_version": 1,
  "repo": "example",
  "skills": {
    "source_precedence": ["codex-product", "repo-claude", "claude-plugin", "global-agents", "codex-only"],
    "preserve_product_managed": true,
    "disabled_roots": [".agents/skills", "${HOME}/.codex/superpowers/skills"],
    "duplicate_choices": {
      "project:*": "repo-claude",
      "skill-creator": "codex-product",
      "superpowers:*": "claude-plugin:superpowers@superpowers-marketplace"
    },
    "project": {
      "root": ".claude/skills",
      "implicit": ["build"],
      "explicit_only": ["release"],
      "unlisted": "disabled"
    },
    "commands": {
      "root": ".claude/commands",
      "explicit_only": ["daily-pipeline"],
      "unlisted": "disabled"
    },
    "global_agents": {
      "root": "${HOME}/.agents/skills",
      "implicit": [],
      "explicit_only": ["dogfood"],
      "unlisted": "disabled"
    },
    "claude_plugins": {
      "root": "${HOME}/.claude/plugins/cache",
      "unlisted_packages": "disabled",
      "packages": [{
        "id": "superpowers@superpowers-marketplace",
        "implicit": ["brainstorming"],
        "explicit_only": [],
        "unlisted": "disabled"
      }]
    },
    "codex_only": {
      "root": ".codex-only/repos/taste-skill/skills",
      "namespace": "taste-skill",
      "unlisted": "disabled",
      "profiles": {"default": [], "design": ["taste-skill:minimalist-ui"]}
    }
  },
  "mcp": {
    "definition_sources": [".mcp.json", "mcp.local.json", "codex-product"],
    "default_profile": "default",
    "mode": "exact",
    "required_in_all_profiles": ["harness-rag"],
    "profiles": {
      "default": {"enabled": ["harness-rag"]},
      "work": {"enabled": ["computer-use", "harness-rag", "jira"]}
    }
  }
}
```

Membership accepts either a skill directory name or the effective `name` in `SKILL.md`. An optional `codex_only.namespace` prefixes unqualified frontmatter names while leaving already-qualified names intact, so a package can expose stable names such as `taste-skill:minimalist-ui` without rewriting its source. Claude plugin versions are resolved from the newest installed cache entry. If two routes have different `SKILL.md` hashes, generation fails unless `duplicate_choices` selects the exact source. Identical duplicates collapse to the manifest-selected route.

`implicit` skills remain model-visible. `explicit_only` skills receive a generated `agents/openai.yaml` with `allow_implicit_invocation: false`, so `$skill` invocation still works without spending startup description budget. Non-canonical and disabled routes receive exact `[[skills.config]]` entries with their resolved `SKILL.md` path. Manifest-selected paths cannot be overridden by stale runtime `skills.config` entries. Unexpected skill directories inside the generated home are moved intact to `.surface-quarantine/skills/` before the exact surface is rebuilt; source stores are never changed.

`skills.commands.explicit_only` is the fail-closed allowlist for portable `.claude/commands/<name>.md` wrappers. Commands stay explicitly callable without entering the prompt. If a command and canonical project skill have the same name, the project skill wins; the resolver never writes command output through the project-skill symlink.

## Profiles

The default profiles come from the manifest. `wh codex work` selects the `work` MCP profile with Codex's base model profile. Select another profile explicitly for one launch:

```bash
HARNESS_CODEX_MCP_PROFILE=work wh codex base
HARNESS_CODEX_SKILL_PROFILE=design wh codex rich
```

An unknown profile, a missing profile-only skill, a missing non-product MCP definition, or a profile that omits `required_in_all_profiles` fails before Codex starts. Every MCP definition is rendered with an explicit `enabled = true` or `false`; profile policies may additionally set tool filters, positive startup/tool timeouts, `required`, default tool approval, and per-tool approval. `computer-use` is the currently recognized product-managed MCP name. When enabled, its bundled plugin skill is reconciled into `skill-catalog.json` after plugin materialization so work-profile prompt audits remain exact.

### Launcher compatibility policy

The manifest remains a generic multi-profile resolver. A project may instead
opt into a launcher-level single-full transition by setting
`HARNESS_MCP_SURFACE_POLICY` in its own `config/launcher.env`; no inherited
environment value can enable it. Empty or omitted preserves the legacy
multi-profile launch behavior.

With `single-full-compat`, a direct Codex `work` selector is accepted with a
warning and resolves to the full user-facing surface. With `single-full`, the
same retired selector is rejected. In both opt-in modes, the launcher clears
`HARNESS_CODEX_MCP_PROFILE`, so `mcp.default_profile` selects the manifest MCP
set. This unifies the surface choice users see for Codex and Claude, without
merging their definition-source systems. Kiro remains outside this retirement:
its native `light` and full selections are still available.

Selected global definitions use the same duplicate detection as project, local, and product-managed sources; any repeated server name fails rather than being overridden. Their supported static fields are intentionally narrow: `env` and `http_headers` are rejected. Use `env_vars`, `env_http_headers`, or `bearer_token_env_var` for runtime credentials.

## Generated state and warm launches

The generated home adds:

```text
skill-catalog.json       selected source paths, hashes, invocation policy, MCP set
surface.config.toml      exact disabled skill paths
.surface-success.json    atomic successful-input fingerprint
.surface-fingerprint-cache.json  source identities and warm watch snapshot
```

The cold fingerprint covers the manifest, relevant launcher code, Claude source/rules/skills/agents/commands, MCP definitions, the normalized selected global-MCP definitions, every installed plugin package's latest skill metadata, and bundled marketplace identity. Skill scripts, references, tests, docs, and assets remain source-linked and do not need regeneration. A lean warm probe validates cached file identities, explicit-only policy files, the semantic TOML skill/MCP/plugin/config policy, managed skill inventory, watched directory topology, bundled product-skill digests, global-MCP digest, and hashes of launcher-owned outputs such as `AGENTS.md` and `hooks.json`. Curated remote-plugin version directories use an exact entry-name snapshot, so a metadata file rewrite is ignored while a new version still invalidates the surface. New unapproved plugins, poisoned markers, hidden MCP tables, changed selected global MCPs, and generated-home drift therefore force full resolution without rescanning plugin payloads. Auth contents, sessions, hook trust state, and generated output mtimes do not invalidate it. A missing managed skill link also forces a rebuild. The live success stamp remains intact while a candidate transaction is built; the replacement success stamp is published last, so an interrupted or failed rebuild cannot advertise candidate state as warm success.

Do not edit generated files. Change the manifest or source and run the launcher again.
