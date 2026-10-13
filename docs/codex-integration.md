# Codex CLI integration

This document describes the maintained native Codex path. It replaces the original implementation plan and reflects the current generated layout and compatibility policy.

## Native Codex versus gateway mode

The command names are intentionally distinct:

```text
<prefix> codex             native OpenAI Codex CLI
<prefix> codex-gateway     Claude Code through a Codex-compatible gateway
```

Native Codex receives a project-scoped `CODEX_HOME`. Gateway mode remains a Claude Code launch and does not use Codex profiles or sessions.

## Binary resolution

The launcher resolves native Codex in this order:

1. `HARNESS_CODEX_BIN`, as an executable path or command name;
2. `codex` from `PATH`;
3. `/Applications/Codex.app/Contents/Resources/codex` only when `HARNESS_CODEX_ALLOW_APP_FALLBACK=1`.

The app fallback is opt-in because the bundled CLI may be older than the terminal installation.

A global `codex` wrapper is also defined after sourcing `aliases.zsh`. When a direct Codex invocation includes `--cd`/`-C` pointing at a registered project, the wrapper prepares the matching project `CODEX_HOME` before delegating to the real binary. Set `HARNESS_LAUNCHER_DISABLE_CODEX_WRAPPER=1` to disable this behavior.

Interactive Zsh can opt that wrapper into PWD-based profile selection with
`harness_shell_enable`. While enabled, plain `codex` delegates to the
package-local `harness-auto`, and explicit `--cd`/`--cd=`/`-C` targets must stay
within the selected harness. The activation state is deliberately not exported
to the child launcher shell. `harness_shell_disable` restores the preceding
wrapper behavior; `command codex` always bypasses both wrapper modes and calls
the executable selected from `PATH`.

The same opt-in installs a shell-local `claude` wrapper. Session-shaped input
uses the selected harness's direct base route. A native Claude management
subcommand in first-token position (`mcp`, `auth`, `plugin`, `doctor`, and the
other commands shown by `claude --help`) instead preserves exact argv and PWD,
loads the selected project's local environment, and bypasses all session-only
launcher mutations including isolation. Claude authentication is still the
native user-global login, not a generated per-profile auth home. Use
`command claude` for an unscoped native launch or when a global option must
precede the management subcommand.

## Generated layout

Before launch, `codex-home-prepare.sh` converges:

```text
<project>/.harness/codex/
├── config.toml
├── fast.config.toml
├── base.config.toml
├── sol.config.toml
├── astra.config.toml
├── plan.config.toml
├── rich.config.toml
├── AGENTS.md
├── auth.json -> ~/.codex/auth.json
├── hooks.json
├── skill-catalog.json
├── surface.config.toml
├── skills/
├── agents/
├── plugins/
├── sessions/
└── history.jsonl
```

The exact set depends on available source files and installed Codex features.
Configuration and adapters are generated; Native sessions and databases are
durable user data sharing the same directory. Both are ignored by Git. See
[Durable native Codex history](codex-history.md) for source-local discovery,
restoration and workspace retirement.

When `config/codex-surface.json` exists, preparation uses its exact skill, Claude-plugin, Codex-only, and MCP allowlists instead of importing every available source. See [Codex surface manifests](codex-surface.md) for schema version `1`, profile selection, and warm-path invalidation.

## Native task progress

Generated homes explicitly set `[tools.update_plan]` with `enabled = true`.
Codex 0.152.0 made this tool opt-in; the footer's `task-progress` item only
displays progress and does not enable the tool. Warm preparation repairs
missing or disabled values. After upgrading, start a fresh session and verify
a native `update_plan` call; existing sessions may retain their old tool catalog.

## Profiles

Codex 0.134.0 and newer load named profiles from `<profile>.config.toml` files with top-level keys. The launcher does not generate legacy `[profiles.<name>]` tables.

```toml
# config.toml
model = "gpt-5.6-terra"
model_reasoning_effort = "medium"

# fast.config.toml
model = "gpt-6-luna"
model_reasoning_effort = "low"

# plan.config.toml
model = "gpt-6.1-sol"
model_reasoning_effort = "high"
sandbox_mode = "read-only"
approval_policy = "on-request"
```

Current routing:

| Profile | Model | Effort | Additional policy | Intent |
| --- | --- | --- | --- | --- |
| default | GPT-5.6 Terra | medium | Runtime defaults | Everyday work — recommended default |
| fast | GPT-6 Luna | low | Runtime defaults | Quick, shallow work |
| base | GPT-5.6 Terra | medium | Runtime defaults | Everyday work — recommended default |
| sol | GPT-6.1 Sol | high | Runtime defaults | Stronger main model — slower |
| astra | GPT-6 Astra | medium | Runtime defaults | Explicit frontier-model selection |
| plan | GPT-6.1 Sol | high | read-only, on-request | Deep planning — slower |
| rich | GPT-6.1 Sol | xhigh | Runtime defaults | Deep work — slowest normal preset |

These profiles are task-oriented operational presets, not OpenAI default-effort claims. This launcher deliberately uses Luna/low for the speed preset, Sol/high for the sol and plan presets, and Sol/xhigh for rich, one step apart like Claude's `opus` (high) and `rich` (xhigh); an unscoped model picker may use a different general starting effort. Effort can still be overridden independently in native Codex. Reserve max or multi-agent ultra execution for exceptional workloads rather than normal profile defaults.

`<prefix> codex fast`, `<prefix> codex sol`, and `<prefix> codex astra`
select their native profiles explicitly. The Profile menu keeps its original
six choices.
The menu reads the generated model and effort; native `--model` and `-c` options
still pass through. Availability is enforced by the selected Codex CLI/account;
the launcher does not issue a paid readiness probe or silently fall back.
`sol` remains Sol. A prior manual Astra edit to generated `sol.config.toml` is
restored on preparation; select `astra` instead for a persistent launch choice.
The opt-in profiles participate in atomic publication and warm-cache drift repair.

The main profile does not downgrade reviewers: reviewer subagents route independently to Sol/medium by default and may explicitly escalate effort for unusually risky work.

Generated homes default to `model_context_window = 272000` and
`model_auto_compact_token_limit = 217600`, compacting at 80% before the GPT-5.6
API's documented 272K long-context pricing boundary. The TUI context toggle or `codex ... 1m` emits
`1000000` with the existing 414000 compact line. Codex clamps either request to
the selected model/account maximum; verify effective context in a native session.

With Codex CLI 0.153.0 or newer, generated homes also enable
`features.context_management.experimental_mode`. Eligible ChatGPT Plus, Pro,
or Pro Lite sessions on the Codex backend use notes and searchable history to
preserve details within a long-running thread. This is separate from
cross-session `memories`. The launcher omits the nested setting for older
clients because Codex 0.152.1 rejects it during bootstrap; API-key sessions,
custom providers, and temporary structured threads remain excluded upstream.

## Launcher MCP surface policy

The project-owned `config/launcher.env` is the sole opt-in authority for
`HARNESS_MCP_SURFACE_POLICY`; a caller's inherited value cannot opt a project
in. Empty or omitted resolves to legacy behavior, preserving the native Codex
`work` selector and its manifest MCP profile.

`single-full-compat` is the transition setting. A direct `work` selector is
accepted with a deprecation warning and launches the full user-facing surface.
`single-full` is the enforced setting: that retired direct selector is rejected.
For either opt-in setting, the launcher clears an inherited
`HARNESS_CODEX_MCP_PROFILE` before preparing Codex. The manifest's
`mcp.default_profile` then selects the enabled Codex MCP set; this does not
replace the generic multi-profile manifest resolver for projects that keep the
legacy policy.

The user-facing surface choice is aligned between Claude and Codex under this
policy. Their definition-source systems are still separate: Claude keeps its
own configuration path, while Codex resolves the manifest and its selected MCP
definition sources. Kiro's native light/full choice is unchanged, including
for opt-in projects.

## MCP translation

Preparation reads, in order:

```text
<project>/.mcp.json
<project>/.mcp.local.json
<project>/mcp.local.json
```

Local files extend the committed file. Duplicate server names are rejected.

`mcp.kiro.local.json` is intentionally absent from this list: it is the Kiro-only overlay, and Codex preparation must not read it.

Supported input forms include HTTP and stdio servers:

```json
{
  "mcpServers": {
    "remote-docs": {
      "type": "http",
      "url": "https://mcp.example.invalid/api"
    },
    "local-docs": {
      "command": "npx",
      "args": ["-y", "@example/docs-mcp"],
      "env": {
        "LOG_LEVEL": "warn"
      }
    }
  }
}
```

Generated TOML uses `[mcp_servers.<name>]` and optional `.env` tables. Authorization headers that reference an environment variable are converted to Codex's environment-variable field rather than storing the bearer value.

Local environment values can be inherited from `.claude/settings.local.json`. Keep that file out of version control and never log its contents.

### Allowlisted global MCPs

`$HOME/.codex/config.toml` remains the sole authority for user-global Codex MCP definitions. A project can project specific definitions only when both boundaries agree:

1. its trusted `config/launcher.env` sets `HARNESS_CODEX_GLOBAL_MCP_ALLOWLIST`, such as `"docs,jira"`; and
2. `config/codex-surface.json` includes `codex-global-allowlist` in `mcp.definition_sources`.

Every native entrypoint (prefix shortcuts, the direct `codex --cd` wrapper, and the TUI) normalizes the setting after loading `launcher.env`: whitespace is trimmed, duplicates preserve first occurrence, and an empty or absent value is unset before `codex-home-prepare.sh` runs. The preparer does not execute `launcher.env` itself. Exact profiles decide which projected names are enabled; a global definition never silently bypasses that profile boundary. A selected global name that duplicates a project, local, or product-managed definition fails closed.

Global definitions accept only portable Codex MCP fields. Static `env` and `http_headers` are rejected; use `env_vars`, `env_http_headers`, or `bearer_token_env_var` references instead. This keeps credentials in the process environment rather than generated TOML.

## AGENTS.md, rules, and hooks

`CODEX_HOME/AGENTS.md` is a generated file, not a symlink. Preparation uses the project's Codex-native rule compiler when available, then falls back to compatible project rules or `CLAUDE.md`. A Codex response-language supplement is appended without changing the source files.

Supported lifecycle hooks are translated through `codex-hook-adapter.sh`. The
PR and harness-main-only guards instead use `codex-pretool-adapter.py`. Native
`Bash` payloads are forwarded once, unchanged, to the canonical guard. Exact
`code_mode_exec` and `exec` payloads are treated as composite JavaScript and
preflight every static `tools.exec_command({ ... })` call against its literal
`cmd` and absolute `workdir`; their omitted nested `workdir` uses the payload's
absolute, existing `cwd`. Dynamic arguments, unknown payload identities, and
invalid payload cwd values fail closed. A composite program without a static
`tools.exec_command` is a no-op; it is never reinterpreted as raw shell. The two
guards are generated with matcher
`^(Bash|code_mode_exec|exec)$`; other Bash hooks remain Bash-only. Ambiguous
JavaScript forms such as aliased or computed exec access, template interpolation,
and dynamic evaluators also fail closed.
Hook parity is intentionally
partial. A hook that depends on Claude-only payloads or lifecycle semantics
should remain unwired until it has a Codex-specific test.

Legacy project-root `.codex` directories can conflict with generated state. The migration/preparation path quarantines stale layouts rather than merging them silently.

## Skills and generated agents

Without a surface manifest, `CODEX_HOME/skills` is a per-skill merge from available sources:

```text
~/.codex/skills
~/.agents/skills
<project>/.claude/skills
<project>/.codex-only/skills
```

Use `.codex-only/skills` when a skill should not appear in Claude Code.

With a surface manifest, only selected routes are linked. Explicit-only skills remain callable but are excluded from implicit prompt matching. Divergent duplicate hashes require an explicit source choice; unselected routes are disabled by exact `SKILL.md` path without deleting their installation.

Portable Claude agent definitions can be converted into Codex agent TOML. Model tiers map by capability:

```text
haiku  → GPT-5.6 Luna, low
sonnet → GPT-5.6 Terra, medium
opus   → GPT-5.6 Sol, medium
```

Generated agent files are output. Edit the source agent definition instead.
Launcher-generated custom roles set `model_context_window = 272000` and
`model_auto_compact_token_limit = 217600` independently of the main profile.
Consequently, selecting the 1M main-session mode retains its `1000000` and
`414000` base-config values, while converted Claude agent roles remain at the
272K/80% compact limit. This applies only to roles generated from
`.claude/agents/*.md`; Codex's built-in and ad-hoc agents have no launcher
generated role file and are not globally capped by this setting. Provider-owned
external roles such as synchronized `glider-*` TOMLs are also preserved rather
than regenerated, so their context remains under the provider's configuration.

## Bundled plugins and browser support

Terminal Codex can materialize supported entries from the bundled OpenAI marketplace:

- Computer Use
- Chrome bridge

The desktop-only Browser plugin remains pruned from terminal project homes. Browser automation can still use a separately configured CDP/browser-harness path.

The Chrome native host has used several executable names across app and cache versions. Discovery prefers the current platform/architecture path and then known legacy names:

```text
extension-host/macos/<arch>/Codex for Chrome
extension-host/macos/<arch>/ChatGPT for Chrome
extension-host/macos/<arch>/extension-host
```

Plugin synchronization rules:

- synchronize the complete marketplace before deriving plugin version or browser hash;
- atomically replace materialized content when any bundled plugin changes;
- serialize shared `~/.codex` writes with `/usr/bin/lockf`;
- trust only exact browser-client SHA-256 values for `node_repl`;
- do not trust the project-writable `CODEX_HOME` or all of `~/.codex` as code paths.

## ChatGPT Apps

ChatGPT Apps and connectors are deny-by-default. Generated project homes set
`[features].apps = false`, so the subsystem does not load and no connector tool
reaches a session.

A project opts in by naming the app ids it needs in its trusted
`config/launcher.env`:

```sh
HARNESS_CODEX_APPS_ALLOWLIST="asdk_app_<id>,asdk_app_<other-id>"
```

The launcher trims and deduplicates supported `asdk_app_...`, `connector_...`,
and `templated_apps_...` ids before every native Codex preparation. The suffix
starts with a letter or digit and contains only letters, digits, `_`, or `-`,
matching the [official app ID format](https://developers.openai.com/plugins/deploy/submission-errors).
Invalid ids fail before preparation; an empty or
omitted value is explicitly unset, so a prior launch or an ambient caller
environment cannot enable an app.

When the list is nonempty the generated `config.toml` turns the feature flag on
and still denies everything that is not named:

```toml
[features]
apps = true

[apps._default]
enabled = false

[apps.asdk_app_<id>]
enabled = true
```

Both layers matter. `[features].apps` decides whether the subsystem loads at
all; `[apps._default]` keeps an app the project never named disabled even while
the subsystem is running. Because `$CODEX_HOME` points at the project home, a
deny-by-default setting in the user's global `~/.codex/config.toml` is not read
and has to be re-asserted here.

The normalized allowlist is part of the managed-home fingerprint. Adding,
replacing, or removing an id regenerates the home once, preserves runtime-owned
auth, hook trust, skill choices, and external plugin state, then returns to the
warm path. The warm validator also rejects an unexpected `[apps.*]` table and
repairs it from the configured allowlist.

## Hook trust in isolated sessions

Fresh manifest-enabled isolated homes inherit an existing canonical hook approval
only when the launcher session record binds the source, physical session root,
inode and base snapshot. Each entry must retain the same event, matcher, handler
position and attributes. Only literal `core/hooks/*.sh` arguments may move from
the canonical root to the isolated root; their bytes must match both the source
and the recorded Git snapshot. The four registered launcher callbacks may move only to the currently verified
package's exact script token when both old and current files match the approved
SHA. A launch-record row ending in `--callback` (ssot profiles) also needs a
`callbacks` entry in the approval grant for
`$HOME/.local/share/harness-service/bin/harness-session-hook` whose sha256
matches the file. Every other argument, matcher and handler attribute remains exact; Orca and
Herdr host commands stay literal. The derived native hash is written inside the existing
atomic config publication, without changing global config or plugin approvals.

A private source approval record under launcher state is captured by an operator
only after the concrete native approval and source audit. It binds the approved
snapshot/base blob provenance, native executable/version, the complete hook set
with a versioned literal-root-normalized digest, and script/callback hashes;
the launcher only reads it and never creates or refreshes approvals. The raw
approved-file digest is audit metadata: new isolated paths necessarily change
those bytes. Enforcement uses the versioned normalized complete hook set and
per-script hashes verified against both approved and current Git base blobs.

Native `--profile rich` saves hook decisions in `rich.config.toml`. Preparation
preserves exact profile hook-state blocks through cold publication and its CAS,
and warm signatures exclude only those mutable blocks. Other profile content
still invalidates the surface and is repaired from launcher policy.

An explicit target decision wins, including disabled hooks. Missing, stale,
unknown, changed or unsupported evidence retains native hook review. Unchanged
warm launches preserve runtime trust. This carries an existing approval across
an equivalent path; it does not enable `--dangerously-bypass-hook-trust` or approve
new hooks. Legacy homes without a surface manifest retain their existing review
behavior. The command fingerprint subset is verified against Codex 0.161.

## Auth behavior

`CODEX_HOME/auth.json` links to the active native Codex auth file. This keeps login selection global while sessions, MCP config, rules, skills, and history remain project-scoped.

The launcher never copies refresh tokens between auth stores. Login and account switching belong to the native Codex CLI or the user's account-management tooling.

## Session commands

Generated homes expose the saved Codex thread name on both native TUI title
surfaces:

```toml
[tui]
terminal_title = ["activity", "thread-title", "project-name"]
status_line = ["thread-title", "model-with-reasoning", "git-branch", "context-remaining", "branch-changes", "run-state", "task-progress", "five-hour-limit", "weekly-limit"]
```

After `/rename <name>`, Codex refreshes the footer and emits the configured
terminal title through OSC 0. The native non-cmux fallback renders activity,
then the thread name, then the repository project name. The footer prioritizes
context remaining ahead of branch changes, and native `update_plan` progress
ahead of account-limit meters. Before a rename, `thread-title` can
fall back to the thread ID. Change these defaults in `codex-home-prepare.sh`,
not in a generated project `config.toml`.

Inside cmux (detected runtime `cmux`; other runtimes never start it), the launcher starts a fail-open title broker under the live
launcher/Codex process ancestry. The short-lived `SessionStart` hook hands the
broker only the exact session ID and Codex owner PID; the broker then replaces
the native composite title with `<thread name> | <profile>`. It reads only that
session ID from the project's
`session_index.jsonl`, targets the exact starting tab surface, and never
renames the workspace. Missing cmux state or thread metadata is a silent no-op.
Because Codex trusts hook commands by hash, review a newly generated or changed
command through `/hooks` when Codex requests it.

```text
<prefix> codex                 new session with base profile
<prefix> codex fast            new session with fast profile
<prefix> codex sol             new session with GPT-6.1 Sol at high effort
<prefix> codex astra           new session with Astra at medium effort
<prefix> codex base 1m         explicit long-context session
<prefix> codex [profile] work   new session with the work MCP surface (any profile)
<prefix> codex continue        source-local history selection
<prefix> codex resume          source-local history catalog
<prefix> codex resume --list   metadata JSON, no Native or model launch
<prefix> codex resume <uuid>   fresh local restore of exact Native history
```

The interactive launcher also supports forking the last Codex session. Extra runtime arguments pass through after launcher parsing.

When native Codex is selected interactively, the Profile menu offers the model
profiles only; the work surface is a `🔌 MCP surface` toggle on the summary
screen (the same UX as the Claude/Kiro `light` toggle) and combines with any
profile. The adjacent `🧠 Context` toggle defaults to `272K (Recommended)` and
can opt into `1M`; shortcuts accept the same `272k`/`1m` keywords before any
free-form Codex arguments. The selection is stored in launch history and passed
to preparation as `HARNESS_CODEX_CONTEXT`.

The 272K mode emits `model_context_window = 272000` and
`model_auto_compact_token_limit = 217600`, so compaction starts at 80% of the
nominal window and remains below the 258400 effective ceiling reported by the
current 95% model metadata. The 1M mode emits `1000000` and `414000`; Codex may
clamp the requested window to the active account/model ceiling. Switching modes
invalidates the exact-surface warm check and regenerates `config.toml` before
launch. `default` leaves `HARNESS_CODEX_MCP_PROFILE` unset and uses the
manifest's minimal default surface; `work` exports
`HARNESS_CODEX_MCP_PROFILE=work` before preparing `CODEX_HOME`, so preparation
and the launched process use the same approved work integrations. The Happy
wrapper and the work surface are mutually exclusive (the later toggle wins).
Backing out never silently upgrades the surface.

The global MCP projection is part of the exact-surface warm fingerprint. A changed allowlist or selected `$HOME/.codex/config.toml` definition triggers cold preparation; unrelated global MCPs do not. `CODEX_HOME` remains project-scoped, so one harness cannot publish another harness's generated MCP state.

## Verification

Generated TOML proves what the launcher intended, but a real session proves what Codex loaded. For model-routing changes:

1. create a disposable project with `config/launcher.env`;
2. run one real Codex request per distinct profile;
3. inspect the session JSONL `turn_context` for `model`, `effort`, and sandbox values;
4. remove the disposable project;
5. confirm no real project gained tracked changes.

Automated coverage lives in:

```text
test/test-codex-home-prepare.sh
test/test-launcher-codex-cli.sh
test/test-launcher-codex-tui.sh
test/test-launcher-codex-gateway.sh
test/test-codex-hook-adapter.sh
test/test-codex-global-mcp-drift.sh
```

## Inspect generated profile settings

Use the installed resolver to inspect a prepared home without running Codex,
refreshing credentials, changing hook approval, or regenerating files:

```bash
python3 "$(brew --prefix harness-launcher)/share/harness-launcher/codex-surface.py" \
  inspect --codex-home /path/to/project/.harness/codex --profile astra
```

The JSON reports allowlisted model, effort, context, sandbox, and approval values
with their base or profile file provenance. Provider URLs, credentials, MCP
arguments, and hook commands are omitted. A missing profile fails instead of
silently displaying the base model.

`output_consistency` compares generated outputs with their last preparation
stamp. It does not validate current source inputs, CLI/environment overrides,
account availability, or the model loaded in a running session. Unknown values
remain unknown; configured context is not a verified native model limit.

New preparation stamps also separate TOML diagnostics:

- `managed_settings_consistency` covers model, effort, policy, and other settings.
- `trust_settings_consistency` tracks project trust separately; a change is never
  treated as harmless UI state or automatically approved.
- `runtime_metadata_changed` tracks only `tui.model_availability_nux`. Unknown
  settings remain part of the managed comparison.

These fields do not replace full output hashes or alter warm-cache repair. A
legacy stamp reports unknown diagnostics until normal preparation writes them.

Homebrew preparation through its compatibility `share` link or a versioned
`Cellar/harness-launcher/<version>` keg uses the equivalent `opt` package path
when both resolve to the same directory (`harness_launcher_stable_dir`; the shell
route through `harness-auto` enters through the keg). This keeps generated hook
command spelling stable across entrypoints and upgrades: Codex asks to trust
every hook whose command changed, so a keg path re-asked after each upgrade, and
a running session's hooks broke once `brew cleanup` removed the old keg. The
Claude launch-record hook in `--settings` uses the same spelling. It does not
approve hooks or redirect a source checkout or a different active keg.

The Codex hook adapter sets `HARNESS_HOOK_RUNTIME=codex` for its child hook only.
Shared hooks should prefer an explicit runtime identity over a stale
`CODEX_HOME` inherited by a Claude session.

### Slack user approval across launch modes

An exact MCP profile may declare a tool allowlist and per-tool approval modes
without changing the native sandbox or global approval policy. If a host-local
server is absent, its policy is omitted only when it explicitly declares
`required: false`; required or malformed definitions remain errors.

An enabled Slack connector must be explicitly identified with
`HARNESS_CODEX_SLACK_APPS="asdk_app_<id>"`; every named id must also be in
`HARNESS_CODEX_APPS_ALLOWLIST`. Other connectors do not inherit this policy.
The managed home and final native launch both set exact public mutation tools
(`send_message`, `schedule_message`, `edit_message`, `delete_message`, file
upload completion, and canvas mutations) to `prompt`, with
`approvals_reviewer="user"`. Raw, `slack_`, and `slack_slack_` tool names are
covered; reads, reactions, and message drafts are excluded.

Caller flags, profiles, and resumed grants cannot disable those prompts. A
Slack-enabled `bypass` launch retains `danger-full-access` but changes approval
to `on-request`; a plain `never` launch keeps its sandbox. Codex auto-approves
MCP tool prompts only under `never` with full disk access, so a final
`danger-full-access` launch (bypass, or a resumed bypass recorded as
`on-request` with `danger-full-access`) also sets
`default_tools_approval_mode = "approve"` for each MCP server and each enabled
non-Slack app in the generated `config.toml`. Without this, every MCP tool call
asked for approval. Slack apps and MCP servers whose name contains `slack` keep
their own policy (a Slack MCP server registered under another name is not
recognized, so name it accordingly); a server or app mode already set in `config.toml` or by a
caller `-c`, and explicit per-tool modes, still win; an unreadable home adds
nothing. The record cannot tell a resumed bypass from an explicit
`-a on-request -s danger-full-access`, so the explicit form is treated the same
way, and the mode is fixed at launch: lowering `/permissions` later does not
bring MCP prompts back. Per-account `links`
policy overrides are cleared only in the final runtime configuration, forcing
every connected account through the app's user reviewer. This does not modify
saved connector credentials or account metadata. The existing stdio app-server
guard also clamps thread/turn grants and rejects protected config writes.

The private Slack opt-in participates in the warm-home identity. Removing it
restores ordinary launcher policy. Native `prompt` approval is not remembered
for later calls, so a changed channel, text, mention, or attachment requires a
new decision. Conversational full-message preview remains the harness skill's
responsibility; the native prompt protects the actual operation. Managed
enterprise policy that requires an automatic reviewer must be reconciled by
its administrator before this user-only contract can be used.

Claude caller settings are merged into one final settings source, retaining
existing fields and hooks while adding mandatory Slack ask rules. When the
caller supplied a settings file, the merge is written to a private `0600`
temporary file and removed when the runtime exits; file-contained environment
secrets are never expanded into command-line arguments.

Happy wrappers are rejected when `HARNESS_CODEX_SLACK_APPS` is nonempty: their
internal settings and approval overrides do not preserve this policy. Use the
native Claude or Codex launcher. Harnesses without this opt-in keep existing
Happy behavior.

## Preserving a running generation

An operational launcher may stage a full verified published-release snapshot
beside an existing Homebrew package, and select that immutable directory for
future UUID-isolated sessions. This is distinct from a Homebrew package upgrade:
the legacy `opt` path and canonical runtime home remain in use by old sessions.
The external selector owns release/tag verification, complete package manifests,
private ownership, resume binding and retention. It must not execute a writable
checkout, guess a missing resume binding or remove a referenced runtime.

Such launches set `HARNESS_CODEX_GLOBAL_PLUGIN_POLICY=preserve`. Preparation
still materializes the new isolated home, but does not publish to the global
bundled marketplace, replace the global Chrome cache/configuration or restart
its native host. For a Chrome-enabled surface, the proposed assets and existing
native-host configuration must match; incompatibility fails with
`global_plugin_incompatible`. It does not silently disable an enabled surface.
This asset/configuration proof does not establish a live browser connection.

Ordinary launches retain `manage`. Snapshot activation does not run the disabled
standalone source installer, rewrite Cellar files, or change launch permissions.
