# harness-launcher

[![CI](https://github.com/kilhyeonjun/harness-launcher/actions/workflows/ci.yml/badge.svg)](https://github.com/kilhyeonjun/harness-launcher/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/kilhyeonjun/harness-launcher)](https://github.com/kilhyeonjun/harness-launcher/releases/latest)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A profile-aware Zsh launcher for Claude Code, OpenAI Codex CLI, and Kiro CLI. Register one or more project directories, give each a short command, and keep runtime state isolated per project.

Optional profile-scoped response collection is documented in
[Native response collection](docs/session-collector.md).

```text
wh             interactive runtime and mode picker
wh base        Claude Code with the base preset
wh codex fast  Codex CLI with the fast profile
wh kiro-cli    Kiro CLI with an isolated KIRO_HOME
harness-auto --explain codex  inspect the current directory's registered profile (v0.31.2+)
```

## Why use it?

AI coding CLIs usually keep sessions, configuration, skills, and MCP servers in a global home directory. That gets messy when you work across projects with different trust boundaries.

`harness-launcher` keeps the command short while preparing project-scoped runtime homes:

```text
<project>/.harness/codex
<project>/.harness/kiro
```

It also provides consistent `fast`, `base`, `opus`, `plan`, `rich`, and `fable` presets, optional gateway routing for Claude Code, tab completion, and an interactive TUI.

Verified operational release snapshots can preserve an older Homebrew runtime
while new isolated sessions use another release. Their selected runtime version
is separate from the installed package version. See
[preserving a running generation](docs/codex-integration.md#preserving-a-running-generation)
for ownership, resume and shared-plugin requirements.

## Requirements

- macOS
- Zsh
- Python 3.11 or newer (`tomllib` is required by the Codex surface validator)
- At least one supported runtime:
  - [Claude Code](https://docs.anthropic.com/en/docs/claude-code)
  - [OpenAI Codex CLI](https://github.com/openai/codex)
  - [Kiro CLI](https://kiro.dev/cli/)

Optional tools:

- [`gum`](https://github.com/charmbracelet/gum) for a richer menu
- [`happy`](https://github.com/slopus/happy) for Happy-managed sessions
- Node.js when using local Claude gateway health checks

## Install

### Homebrew

```bash
brew tap kilhyeonjun/tap
brew install harness-launcher
```

Upgrade later with:

```bash
brew update
brew upgrade harness-launcher
```

### From source (disabled)

Standalone source installation is disabled due to an unfixable TOCTOU vulnerability in portable shell installers. Use Homebrew instead. Existing source installations should migrate:

```bash
brew tap kilhyeonjun/tap
brew install harness-launcher
# Then remove the old source-installed files from $HARNESS_LAUNCHER_PREFIX
```

## Quick start

A registered project needs `config/launcher.env`:

```bash
mkdir -p "$HOME/work-harness/config"
cat > "$HOME/work-harness/config/launcher.env" <<'EOF'
HARNESS_NAME="Work harness"
HARNESS_PREFIX="wh"
EOF
```

### MCP surface policy

`config/launcher.env` is the only authority that can opt a project into the
single-full policy. An inherited shell value is ignored. Leave
`HARNESS_MCP_SURFACE_POLICY` empty or omit it to preserve the legacy behavior:
Claude and Kiro can select `light`, and native Codex can select the manifest's
`work` MCP profile.

Projects can choose one of these opt-in values in `config/launcher.env`:

```bash
# Temporary compatibility phase: accept retired direct selectors with a warning.
HARNESS_MCP_SURFACE_POLICY="single-full-compat"

# Enforced phase: reject retired Claude/Codex direct selectors.
HARNESS_MCP_SURFACE_POLICY="single-full"
```

Both opt-in values use the full user-facing surface for Claude and Codex. In
the compatibility phase, direct `light` (Claude) and `work` (Codex) selectors
continue as full with a deprecation warning; the enforced phase rejects those
selectors. Kiro remains intentionally independent: its native `light` and
full choices continue to work in every policy. An invalid value fails before
the project command is registered.

Add the launcher and project registration to `~/.zshrc`. Use the source line that matches how you installed it.

Homebrew:

```zsh
source "$(brew --prefix harness-launcher)/share/harness-launcher/aliases.zsh"
harness_register "$HOME/work-harness"
```

Source install with `HARNESS_LAUNCHER_PREFIX="$HOME/.local"`:

```zsh
source "$HOME/.local/share/harness-launcher/aliases.zsh"
harness_register "$HOME/work-harness"
```

Start a new shell and verify the command:

```bash
exec zsh
wh codex --version
```

`HARNESS_PREFIX` becomes the shell function name. Register as many projects as you need, but each prefix must be unique. To make the same prefix available as a real executable outside interactive Zsh startup, register it once:

```bash
harness-profile register "$HOME/work-harness"
```

This installs a profile entry under `~/.config/harness-launcher/profiles/` and a command symlink under `~/.local/bin/` without copying project policy.

Registered commands are workspace-aware. If the current directory resolves inside the owning harness, the launcher uses it automatically; otherwise it preserves the legacy harness-root default. An explicit `--cwd` still takes precedence.

### Isolated sessions

Root sessions can opt into a disposable detached Git repository with `--isolated`, or by
setting `HARNESS_SESSION_ISOLATION=1`. The launcher exports
`HARNESS_SOURCE_ROOT`, `HARNESS_SESSION_ROOT`, and `HARNESS_RUN_DIR`; a root
launch runs from the remote-free session repository while an explicit product `--cwd` stays
in that product worktree. Session records are written atomically below
`${XDG_STATE_HOME:-$HOME/.local/state}/harness-launcher`. Machine-local
`config/.local` is never copied, and `projects/` is linked back to the
canonical harness so existing product worktrees remain available. Claude
plugins installed with `--scope project` or `--scope local` for the canonical
harness also load in the session: the launcher mirrors their install records
for the session root (see [Claude plugins in isolated sessions](docs/architecture.md#claude-plugins-in-isolated-sessions)).
Native Codex plugins that both the canonical and session `config/codex-surface.json`
list in `runtime_audit.native_codex_plugins` are carried from the canonical
`.harness/codex` when installed and enabled there, without hook trust or auth
(see [Native plugins in isolated sessions](docs/codex-integration.md#native-plugins-in-isolated-sessions)).

A profile can set `HARNESS_SESSION_ISOLATION_DEFAULT=1` to route fresh,
interactive direct-Claude and native-Codex launches into isolation by default.
A bare interactive profile command chooses a workspace before the launcher
collects runtime options: isolated is the default and the current workspace is
an explicit shared-session choice. Batch, help, diagnostics, and gateway/Kiro
remain on the legacy path. Claude continuation and fork commands require the exact
UUID shown at session start, or explicit `--no-isolated` for canonical history. This
profile setting is a bounded canary switch; it does not change other profiles.

Codex history is discovered locally across source-bound canonical, isolated and
archived homes. `ex codex resume --list` returns metadata JSON without a model
or network call; `ex codex resume <native-uuid>` restores history into a fresh
local workspace. Completed Git journals keep their original state. A request
with several possible histories needs an exact UUID in a noninteractive shell.
Native data is archived separately from disposable roots, and GC retains a root
until a characterized Native binary verifies the restored full history. See
[Durable native Codex history](docs/codex-history.md) for limits and recovery.

Release-owned entries can verify an explicit Claude fork without inheriting the
parent grant or reopening a completed owner; see
[Explicit Claude fork verification](docs/terminal-runtimes.md#explicit-claude-fork-verification).

The bundled `session-isolation.sh` can submit a session's immutable patch and
manifest, then integrate it through a locally serialized remote fast-forward
lane. It applies the patch three-way on the current remote tip, runs the
repository-owned verifier with post-commit side effects disabled, retries once
after a remote advance, records the successful non-force push, and verifies the
exact delivered commit's path/mode/blob manifest from a fresh clone of remote
history before marking `DELIVERED`. A later remote descendant does not turn an
already accepted delivery into a false conflict. Homebrew exposes the lifecycle command
as `harness-session`:

```bash
ex --isolated codex base
harness-session list
ex --isolated-session <uuid> codex resume
ex --no-isolated resume
harness-session close <uuid>
```

`close` snapshots all tracked and untracked session changes and binds the
canonical source and its `origin` into the submission digest. The caller cannot
replace the verifier or remote. Closing or normally exiting a clean session
records terminal `CLOSED` without creating an empty submission. A failed
apply, verifier, bounded remote retry, or verified readback mismatch becomes
durable `CONFLICT`; a post-push readback outage remains `INTEGRATING` until
recovery proves whether the pending commit reached remote history.
`harness-session recover <uuid>` reconciles an indeterminate integration or
reopens the same workspace for repair. `harness-session discard <uuid>` retires
an `ABANDONED` or `CONFLICT` session whose work will not be delivered: it keeps
the work as `discarded.patch` in the session record, then marks it terminal
`DISCARDED`.

Only one launcher may own a UUID at a time. A kernel lease covers the complete
runtime and is not inherited by Claude, Codex, heartbeat, or title-broker child
processes. `CLOSED` workspaces remain resumable for 24 hours by default;
launcher startup garbage collection retires expired `CLOSED`, `DELIVERED` or
`DISCARDED` roots only after acquiring that lease. Journals remain for audit. Set
`HARNESS_SESSION_RETENTION_SECONDS` to a validated value from `0` through
`604800`; `0` makes a terminal root eligible on the next collection.

Workspace managers that need to choose the profile instead of naming it can use `harness-auto`. It resolves the current directory against the private profile registry, selects the single most-specific owning harness, and fails closed outside or across ambiguous boundaries:

```bash
harness-auto codex base
harness-auto claude base
harness-auto kiro-cli base
```

This is the supported command override for Orca's built-in Claude, Codex, and Kiro agent entries. It does not infer a profile from repository names or remotes; the worktree must live below its registered harness boundary.

Orca-specific behavior (v0.33.0+) is described in [Orca ADE integration](docs/orca-integration.md): `CODEX_HOME` sanitization inside Orca terminals, the opt-in `HARNESS_ORCA_AGENT_HOOKS=1` Codex status hook in a harness's `config/launcher.env`, and mapping Orca's `--resume <id>` restore to isolated sessions.

Terminal-runtime behavior (v0.36.0+) is described in [Terminal runtimes](docs/terminal-runtimes.md): the `herdr > orca > cmux > plain` detection order, per-launch environment scrubbing, herdr `OSC 7` directory reports, the Codex status-hook registry, and restore routing for `/clear`ed Claude sessions.

Interactive Zsh users can opt plain `codex` and `claude` commands into the
same location-based routing after every harness has been persisted once with
`harness-profile register`:

```zsh
source "$(brew --prefix harness-launcher)/share/harness-launcher/aliases.zsh"
harness_shell_enable

cd /path/inside/a/registered/harness
codex                 # location selects the registered profile
claude                # equivalent direct-Claude base launch
claude mcp list       # native management command, same selected project/PWD
```

The opt-in is shell-local and fails closed outside registered boundaries.
Plain commands keep native argv (v0.35.0+): arguments go after
`--passthrough`, so `claude rich` passes `rich` as a prompt and `codex -a never`
is a Codex option. Use the profile command (`<prefix> rich`) for launcher
presets. `codex --version`, `-V`, and `--help` run native Codex anywhere.
Codex `--cd`/`-C` targets (every form) must remain inside the profile selected
from the shell's current directory. Claude management commands (`mcp`, `auth`,
`plugin`, `doctor`, and the other native first-token subcommands) keep the
selected project's working directory and local environment, but bypass session
presets, model/effort injection, and session isolation. Claude authentication
itself remains native and user-global; the selected project controls project
settings and MCP definitions, not a separate login. Put the subcommand first;
for option-before-subcommand forms such as `claude --debug mcp list`, use the
explicit native escape `command claude`. `command codex` also bypasses routing,
and `harness_shell_disable` restores the previous plain-command behavior. The
enable step refuses to overwrite an existing Claude alias/function or a
replaced Codex function. Add
`harness_shell_enable` to `.zshrc` after the source and registration lines for
persistent activation, then start a new shell (or re-source `.zshrc`).

For explicit read-only disk diagnostics, run `harness-session report --json`.
It explains retained roots and unknown verification without running GC. Add
`--sizes` for bounded metadata-only size observations; these figures do not
establish physically reclaimable space. See [session disk diagnostics](docs/session-report.md).

The release also includes `bin/harness-plan`: a side-effect-free JSON interface to the validated model, effort, context and permission plan used by managed workspace controls. See [managed workspace creation](docs/herdr-web-ui.md#managed-workspace-creation-and-previous-conversations). It renders a plan; starting an agent still requires an explicit launch.

External orchestrators and non-interactive shells can bypass `.zshrc` while keeping the same project policy:

```bash
harness-exec "$HOME/work-harness" codex base
```

When the external terminal starts inside the harness, no `--cwd .` is needed. If supplied, `--cwd` must resolve inside the registered harness. This is the supported boundary for Orca and similar worktree managers; see [Orca ADE integration](docs/orca-integration.md).

An explicit leading `--isolated` or `--isolated-session <uuid>` defaults to the
session root without adopting the caller's directory. This also applies when a
session manager precreates a UUID and opens the no-argument launcher menu.
An explicit `--cwd` retains precedence and must pass the registered boundary.

Bridges that run one unattended task at a time use `harness-headless`. It never prompts, keeps only an environment allowlist, runs Claude in a fresh isolated session under a launcher-owned sandbox and permission denies (caller settings may only add restrictions or allowed network domains), delivers a successful change through `harness-session close`, and always writes one JSON result:

```bash
harness-headless <profile> --prompt-file task.md --result-file result.json \
  --lock-file run.lock --budget-usd 5 --timeout-min 60 [--settings-file extra.json] [--model sonnet] \
  [--effort low|medium|high|xhigh]
```

`--agent codex` runs Codex in the same isolated session and delivery path instead, under a launcher-generated Seatbelt profile, with model requests through a per-run loopback forwarder that alone holds the endpoint key. Codex must have `codex-code-mode-host` beside it, as the npm vendor build does; set `HARNESS_CODEX_BIN` to that binary:

```bash
harness-headless <profile> --agent codex --model gpt-6.1-sol --effort medium \
  --model-endpoint http://127.0.0.1:<port>/v1 \
  --endpoint-key-file ~/.config/harness-launcher/cliproxy-headless.key \
  [--max-model-requests 400] --prompt-file task.md --result-file result.json \
  --lock-file run.lock --budget-usd 5 --timeout-min 30
```

`--target <name>` (Codex only) runs the same Codex agent on a registered personal code repository instead of the harness and delivers a successful change as a draft pull request on a new `loop/<task>-<approval8>` branch. The owner registers each repository once from a terminal; the caller passes the sha256 of that record, so a changed registry entry is refused until it is registered again:

```bash
harness-profile target add <name> --from targets.yaml   # needs a TTY; shows the entry and its diff, asks for the name again
harness-profile target list
harness-profile target show <name>
harness-headless <profile> --agent codex --model gpt-6.1-sol --model-endpoint http://127.0.0.1:<port>/v1 \
  --endpoint-key-file ~/.config/harness-launcher/cliproxy-headless.key \
  --target <name> --target-digest <sha256> --task-id <id> --approval-sha <sha> \
  --prompt-file task.md --result-file result.json --lock-file run.lock --budget-usd 5 --timeout-min 30
```

The owner's GitHub account, SSH host aliases and deny lists come from `~/.config/harness-launcher/target-policy.json` (mode 0600), which the owner writes; without it every target command is refused. The terminal check in `target add` is an accident guard, not a security boundary; the boundary is that no sandboxed run can write `~/.config/harness-launcher`. See [Headless target mode](docs/architecture.md#headless-target-mode-code-repositories).

The result `status` is one of `delivered`, `pr_opened` (`--target` only), `no_changes`, `conflict`, `failed`, `timeout`, `budget`, or `refused`. See [Headless isolated runs](docs/architecture.md#headless-isolated-runs) for the contract.

SDK hosts that append their own argv to a command prefix put it after `--passthrough`, which ends launcher keyword parsing and lets explicit caller options override the launcher defaults (`--model`, `--effort`, and `--permission-mode` for Claude; `-p`/`--profile` and `-C`/`--cd` for Codex):

```bash
harness-auto claude base --passthrough --permission-mode plan --effort high
harness-codex --profile wh app-server          # Codex executable for SDK hosts
harness-paseo sync --reload                    # Paseo providers from the profile registry
harness-herdr-web install                      # herdr web ui, local-only behind a token
harness-herdr-web usage                        # Claude/Codex plan usage, pace and exhaustion time
harness-herdr-web usage --watch                # the same as a live board for a herdr popup
```

A plain Codex `app-server` runs without `-p` behind a JSON-RPC guard that keeps every thread working directory inside the selected harness. See [Paseo integration](docs/paseo-integration.md).

> [!WARNING]
> `config/launcher.env` is sourced as shell code. Only register project directories you trust.

## Commands

The same command shape works for every registered prefix:

```text
<prefix>                         interactive TUI
<prefix> fast|base|opus|plan|rich Claude Code preset
<prefix> fable                   Claude Code fable + high (direct only)
<prefix> ultracode               Claude Code opus[1m] + xhigh (direct only)
<prefix> continue|resume         Claude Code session shortcut
<prefix> light                   Claude Code with the light MCP surface (SSH-backed servers excluded)
<prefix> codex [profile] [272k|1m] native Codex CLI; 272K default, 1M opt-in
<prefix> codex --app <id> [profile]  enable one ChatGPT app for this launch only
<prefix> codex [profile] work    native Codex CLI with the work MCP surface (any profile)
<prefix> codex continue          Codex `resume --last`
<prefix> codex resume            Codex resume picker
<prefix> codex fork              Codex `fork --last`
<prefix> codex full-auto|never|bypass   Codex safety level (bypass disables sandbox — dangerous)
<prefix> kiro-cli [mode]         native Kiro CLI
<prefix> kiro-cli light          native Kiro CLI with the light MCP surface
<prefix> kiro-cli model=<id>     native Kiro CLI on one catalog model (bare ID also works)
<prefix> kiro-cli effort=<level> native Kiro CLI effort: low|medium|high|xhigh|max
<prefix> kiro [mode]             Claude Code through a Kiro gateway
<prefix> codex-gateway [mode]    Claude Code through a Codex gateway
<prefix> checkup prompt-audit [preset] [--max-budget-usd N]
                                 headless Claude Code `/checkup prompt-audit` at the harness root
harness-profile checkup prompt-audit (--all | <prefix>...) [--mode <preset>] [--max-budget-usd N]
                                 run the checkup for each selected profile, one at a time
```

Extra arguments pass through to the selected runtime.

Native Kiro accepts a granular pair as well as a preset. `model=<id>` (or a bare
catalog ID such as `claude-opus-4.8`) and `effort=<low|medium|high|xhigh|max>`
override either half, in any argument order, so a one-off combination needs no
new preset — `wh kiro-cli plan effort=medium` keeps the preset's model and lowers
only the effort. The Kiro CLI validates `--model` itself (it errors with the live
catalog), but it silently swallows an unknown `--effort`, so the launcher rejects
an out-of-enum level before exec. The TUI mirrors the Claude custom path: the
Kiro mode menu ends with `🔧 Custom`, which lists the runtime's model catalog
with each model's credit multiplier, then the effort enum with the recommended
level marked. The catalog comes from `kiro-cli chat --list-models` (a local
listing — no API call) cached for a day under
`${XDG_STATE_HOME:-~/.local/state}/harness-launcher/kiro-models.tsv`;
`HARNESS_KIRO_CATALOG_TTL` changes the window and `HARNESS_KIRO_MODEL_CATALOG`
overrides the list outright. If the listing fails the launcher serves the last
cache, or a built-in list, and suppresses retries for five minutes
(`HARNESS_KIRO_CATALOG_FAIL_TTL`) so an offline session never waits twice.

Run the prefix without arguments for the TUI. The top screen is a **launchpad**:
your recent launch configurations (up to 8, newest first, deduped per harness in
`.harness/launcher-history`) plus one "New …" composer entry per installed
runtime, fuzzy-searchable under gum — type to filter, Enter to launch. Picking a
history row relaunches that exact configuration; gateways, generated homes, and
MCP configs are revalidated on every replay. The composer collects session and
mode, then shows a single summary screen where permission mode, Chrome, the MCP
surface (`full`/`light` — light drops SSH-backed servers: `start-ssh-mcp.sh`
stdio wrappers and loopback HTTP on ports 38200–38299), and the Happy wrapper
are toggles. Esc (or `q`/invalid-then-`q` in the no-gum fallback) goes one step back; at the launchpad it exits.
Menu labels are generated from the same mode table the shortcuts use, and the
native-Codex profile labels read the generated profile configs, so what a label
says is what launches. Native Codex exposes its `work` MCP surface the same way
Claude/Kiro expose `light` — a `🔌 MCP surface` toggle on the summary screen,
combinable with any profile: `default` keeps the minimal project surface, while
`work` uses only the approved work MCPs declared by `config/codex-surface.json`.
The same screen exposes `🧠 Context`: cost-conscious `272K` is the recommended
default, while `1M` is an explicit opt-in preserved in launch history.

Native Codex homes explicitly enable the `update_plan` task tracker, including
on Codex 0.152.0+ where it defaults to disabled. See [native task progress](docs/codex-integration.md#native-task-progress).

### Presets

| Preset | Claude Code | Codex CLI | Intended use |
| --- | --- | --- | --- |
| `fast` | Haiku, low effort | GPT-6 Luna, low effort | Small edits and quick checks |
| `base` | Sonnet | GPT-5.6 Terra, medium effort | Everyday work — recommended default |
| `sol` (Codex only) | — | GPT-6.1 Sol, high effort | Stronger main model — slower |
| `fable` (Claude direct only) | Fable, high effort | — | Explicit frontier-model selection |
| `astra` (Codex only) | — | GPT-6 Astra, medium effort | Explicit frontier-model selection |
| `plan` | Opus Plan | GPT-6.1 Sol, high effort, read-only | Investigation and planning |
| `opus` (Claude only) | Opus, high effort | — | Strong main model without `rich`'s xhigh cost |
| `rich` | Opus | GPT-6.1 Sol, xhigh effort | Deep work — slowest normal preset |

These are task-oriented operational presets, not claims about OpenAI's model defaults. The launcher deliberately lowers `fast` for speed and raises `plan`/`rich` for deeper work; an unscoped model picker may use a different general starting effort. Model names follow the capabilities exposed by the installed runtime. Generated homes default to a 272,000-token window with a 217,600-token compact threshold; explicit 1M mode requests 1,000,000 with a 414,000 threshold. Launcher-generated custom subagent roles always override to 272,000 with a 217,600-token compact threshold, so a 1M main session does not widen each generated role. Codex model metadata determines the effective window, and neither setting is Astra-specific tuning.

Use `<prefix> fable` for Claude Code's opt-in `fable` alias with `high` effort, or select Fable in the direct TUI preset or Custom model menus. Existing numbered choices stay stable; the Fable preset is appended after Custom. Effort tokens still override the preset, for example `<prefix> fable xhigh`. The launcher rejects this preset on Kiro and Codex gateways. The alias follows the installed Claude Code runtime and any `ANTHROPIC_DEFAULT_FABLE_MODEL` override; availability remains account-dependent. As of September 8, 2026, Claude Code v2.1.255+ resolves it to Fable 5.1 by default ([official model configuration](https://code.claude.com/docs/en/model-config)).

Use `<prefix> codex fast`, `<prefix> codex sol`, or `<prefix> codex astra` for explicit native profiles. The default remains Terra; `fast` uses GPT-6 Luna and `sol` uses GPT-6.1 Sol. Do not rename models inside generated profile files: preparation restores launcher-owned profiles. Availability and the loaded model/effort must be verified in the selected Codex account. No API probe or silent model fallback is added.

The main profile does not downgrade reviewers: reviewer subagents route independently to Sol/medium by default and may explicitly escalate effort for unusually risky work.

### Configuration checkup

`<prefix> checkup prompt-audit` runs Claude Code's built-in `/checkup prompt-audit`
(an alias of `/doctor prompt-audit`) without a TTY, always from the harness root,
so the audit covers that project's instruction files plus the user-level
`~/.claude` skills, commands, agents, output styles, rules, and plugins. The audit
proposes edits; it never applies them.

The run is read-only by construction:

- `--restricted` ignores user, project, and local settings files, so hooks,
  allow rules, and default permission modes from those files do not apply, and
  `--strict-mcp-config` starts no MCP servers.
- Tools are limited to Read, Grep, Glob, Bash, and Agent under `dontAsk`, and
  Edit, Write, NotebookEdit, WebFetch, and WebSearch are denied. The allow list
  adds only `git ls-files` and `git check-ignore`, which neither write files,
  run commands, nor print file contents. Any other Bash command runs only if
  Claude Code's own read-only check accepts it: `head`, `git log`, `git blame`,
  and `git show` work inside the working directories, while write or
  other-file options (`git log --output`, `git blame --contents`) and paths
  outside them (including `git -C` and `--git-dir`) are refused. `git blame`
  is kept off the allow list because an allow rule would also accept
  `--contents <any file>`.
- File access is confined to the harness plus the existing `~/.claude`
  subdirectories above; the `~/.claude` root, which holds settings and
  credentials, is not added. Inside the harness, at the root and in nested
  directories such as worktrees, reads of `.claude/settings*.json`, the
  `.mcp*.json` and `mcp*.local.json` files, `config/.local/`, and `.harness/`
  (including earlier reports) are denied.
- Claude reads no standard input, and the caller's Claude session, messaging,
  terminal, and telemetry variables (`CLAUDECODE`, `CLAUDE_CODE_*` session
  variables, `OTEL_*`, `CMUX_*`) are removed, so running the checkup from inside
  another Claude session does not link the two. The caller's gateway routing
  (`ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_DEFAULT_*_MODEL`,
  `ANTHROPIC_CUSTOM_HEADERS`) and `GH_TOKEN` are removed before the harness's
  own local environment is loaded. `ANTHROPIC_API_KEY` is kept.
- The session is not saved, so `<prefix> continue` at the root never resumes
  the audit.
- A working-tree check compares `git status` entries before and after the run.
  If an entry outside the report directory appears or disappears, the status
  line ends with `tree_changed=N` and a warning goes to standard error. It does
  not see new edits to files that were already modified or changes to ignored
  paths, and another session writing to the same tree triggers it too, so it is
  a signal rather than a boundary and does not change the exit status.

The default preset is `opus`; pass another preset (`fast`, `base`, `rich`,
`fable`) as the next argument. `plan` is not accepted because opusplan runs as
Sonnet outside plan mode. Spending stops at `--max-budget-usd` (default 20, or
`HARNESS_CHECKUP_MAX_BUDGET_USD`); Claude Code checks the limit between turns, so
a run can end slightly above it.

The raw JSON result, Claude's standard error (kept only when non-empty), and the
report are written with private permissions to
`.harness/reports/checkup/prompt-audit-<UTC timestamp>-<pid>.{json,stderr,md}`
inside the harness. Standard output is a single status line — report path, cost,
duration, turns, and the number of permission denials, never report text —
so `harness-profile checkup prompt-audit --all` can run several profiles from one
terminal without mixing their content. `harness-profile checkup` validates every
selected profile before the first run, runs each profile once even if it is named
twice, and applies the budget to each run, so N profiles can spend up to N times
the budget. A run takes several minutes; start a long fan-out in its own terminal
or in the background. Profiles that isolate sessions by default run the checkup
at the root; `--isolated` with `checkup` is rejected before any session is
created. Plugins enabled in user settings are not loaded under `--restricted`;
the audit reads their files from `~/.claude/plugins` instead.

## Project layout

A typical registered project looks like this:

```text
work-harness/
├── config/
│   ├── launcher.env
│   ├── codex-surface.json      # optional exact Codex runtime allowlists
│   └── .local/                 # optional, never commit secrets
├── .claude/
│   ├── skills/                 # shared Claude/Codex-compatible skills
│   └── settings.local.json     # optional local environment values
├── .codex-only/
│   └── skills/                 # project skills exposed only to Codex
├── .mcp.json                   # optional committed MCP definitions
├── .mcp.local.json             # optional local MCP definitions
└── .harness/                   # generated runtime state; gitignore this
```

The launcher merges `.mcp.json`, `.mcp.local.json`, and `mcp.local.json`. Duplicate MCP server names fail fast instead of silently overriding one another.

For a harness-owned stdio script, write an explicit root-relative path such as `"${HARNESS_ROOT}/core/scripts/rag-cli.sh"` in the MCP `command` or `args`. Claude, Codex, and Kiro receive a checked absolute path even when launched from a nested `projects/` directory. Bare relative script paths for `bash`, `sh`, `zsh`, `python`, and `node` are rejected before launch with a migration hint; the generated runtime files are not another configuration source.

Kiro preparation additionally reads `mcp.kiro.local.json`. No other runtime reads that file, so a server that should reach Kiro alone is declared once there instead of being suppressed for Claude and Codex afterwards. The duplicate rule still applies across all four files.

Kiro CLI ignores `headers` on http transport and falls back to OAuth discovery, so preparation rewrites an authenticated HTTP server into an equivalent `mcp-remote@latest` stdio bridge that carries those headers. `${VAR}` in a header is passed through unresolved on purpose: the bridge resolves it from the inherited session environment, so the credential reaches neither argv nor a generated file. An HTTP server without headers is left as-is.

## Codex integration

Before each native Codex launch, `bin/codex-home-prepare.sh` prepares an isolated `CODEX_HOME` under `<project>/.harness/codex`:

- `config.toml` with project MCP servers and the default Terra/medium route
- `fast.config.toml`, `base.config.toml`, `sol.config.toml`, `plan.config.toml`, `rich.config.toml`, and `astra.config.toml`
- generated `AGENTS.md`
- exact manifest-selected skills and MCP flags, or legacy merged links when no surface manifest exists
- project-scoped sessions and history
- an `auth.json` symlink to the active native Codex login

To expose selected user-global MCP definitions, set `HARNESS_CODEX_GLOBAL_MCP_ALLOWLIST` in the trusted project `config/launcher.env`, for example `HARNESS_CODEX_GLOBAL_MCP_ALLOWLIST="docs,jira"`. The launcher trims and deduplicates names before every native Codex preparation; an empty or omitted value is explicitly unset, so a prior launch or caller environment cannot add global MCPs. Definitions remain authoritative in `$HOME/.codex/config.toml`, while the project surface manifest must explicitly opt into `codex-global-allowlist`.

ChatGPT Apps and connectors follow the same shape but stay off unless requested: `HARNESS_CODEX_APPS_ALLOWLIST="asdk_app_<id>"` turns on `[features].apps` and enables only the named ids, while `[apps._default]` keeps every other app disabled. Omitting the variable leaves `[features].apps = false`, so no connector tool reaches a session. Changing or removing the list regenerates the managed home before the next native Codex launch.

For an enabled Slack connector, also set `HARNESS_CODEX_SLACK_APPS="asdk_app_<id>"` in that harness's `config/launcher.env`. Slack message sending, scheduling, editing, deletion, file sharing, and public canvas changes then require native **user** approval of the exact tool arguments on every call. Reads, reactions, and drafts keep their existing behavior. The launcher enforces this after resume, profiles, and caller overrides: Codex `bypass` keeps unrestricted filesystem access but uses `on-request`; `never` becomes `on-request` without widening its sandbox. Claude launches add explicit Slack `permissions.ask` rules even in `bypassPermissions`, preserving supplied settings and hooks. No private app ids are bundled in this package. Slack-enabled harnesses reject Happy wrappers because they cannot preserve the native approval policy; use native Claude or Codex instead.

For a one-shot opt-in, use `<prefix> codex --app asdk_app_<id> [profile]`. The
validated id is merged with that harness's trusted default allowlist for this
launch only; it is not written to `config/launcher.env` and is absent again on
the next launch unless explicitly requested.

The terminal `codex` from `PATH` is preferred. Set `HARNESS_CODEX_BIN` for an explicit binary. Codex.app's bundled CLI is only used when `HARNESS_CODEX_ALLOW_APP_FALLBACK=1` because app bundles can lag behind the terminal release.

Browser support keeps the terminal-safe `browser-harness` path, materializes supported bundled plugins, and uses an exact browser-client SHA allowlist for `node_repl`. Shared plugin cache updates use the macOS kernel lock (`lockf`) so concurrent project launches cannot corrupt global cache state.

See [Codex integration](docs/codex-integration.md) for the generated layout, configuration translation, plugin policy, and compatibility notes.
For large installations, see [Codex surface manifests](docs/codex-surface.md) for duplicate collapse, explicit-only skills, exact MCP profiles, and the warm prepare path.

## Local configuration and secrets

Keep machine-specific gateway URLs, API keys, and MCP credentials out of Git:

```text
config/.local/kiro-gateway.env
config/.local/codex-gateway.env
.claude/settings.local.json
.mcp.local.json
mcp.local.json
mcp.kiro.local.json
```

Use environment-variable references in committed MCP configuration when authentication is required. Never paste credentials into bug reports, logs, screenshots, or pull requests.

Copy the relevant entries from [the project `.gitignore` template](templates/project.gitignore) into each registered project's existing `.gitignore`. Do not overwrite a project's existing ignore rules.

Read [Security](SECURITY.md) before changing auth, MCP, plugin, or runtime-home behavior.

## Troubleshooting

Common fixes are collected in [docs/troubleshooting.md](docs/troubleshooting.md), including:

- a prefix shadowed by an existing alias
- a login shell selecting a stale mise-managed Codex binary
- Codex.app fallback behavior
- duplicate MCP names
- generated state that needs regeneration
- Chrome native-host compatibility

When reporting a bug, include the launcher version, macOS version, Zsh version, selected runtime version, command shape, and a redacted error message.

## Development

Clone the repository and run the full suite:

```bash
git clone https://github.com/kilhyeonjun/harness-launcher.git
cd harness-launcher
./test/run-all.sh
```

The suite dispatches each test through its declared Bash or Zsh interpreter.
Codex surface tests run new/unlisted cases first in a serial gate. Reviewed
publication, signal, auth, and revocation cases then stay serial with each other
in one subprocess while 36 reviewed private-HOME metadata/configuration cases
run in up to three shards. Every integration test keeps its own repo, generated
home, lock, and compiler counter; the prepare and publication assertions are
unchanged. New tests remain in the serial gate until reviewed in
`test/surface_test_runner.py`. A failing gate prevents all reviewed groups from
starting.

Use `HARNESS_SURFACE_TEST_JOBS=1 ./test/test-codex-surface.sh` for the original
serial unittest command. The default is three private shards plus the reviewed
serial subprocess (up to four group subprocesses); `1` and `2` remain supported,
and larger values are rejected. The runner reports group and total wall time,
planned versus executed test counts, and each group's failure output. `--report
PATH` writes machine-readable group and individual-test wall times when invoking
the Python runner directly. Host load varies, so measured times are not a speed
guarantee.

The Codex home integration suite runs its config/skills, hooks, and generated
surface groups with isolated temporary homes. The default is three concurrent
groups; use `HARNESS_HOME_PREPARE_TEST_JOBS=1` to run them sequentially.
`./test/test-codex-home-prepare.sh --report PATH` writes group wall times as
JSON. On one same-host comparison, isolation plus bounded concurrency reduced
wall time from 132.5 seconds to 19.9 seconds; this is an observation, not a
speed guarantee.

Run syntax checks as well:

```bash
bash -n bin/*.sh test/*.sh
zsh -n bin/aliases.zsh bin/*.sh test/*.sh
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for project scope, test expectations, portability rules, and the pull request checklist.

## Documentation

- [Public-safe contract demo](https://github.com/kilhyeonjun/harness-launcher-demo)
- [Documentation index](docs/README.md)
- [Architecture and trust boundaries](docs/architecture.md)
- [Codex integration](docs/codex-integration.md)
- [Terminal runtimes](docs/terminal-runtimes.md)
- [Paseo integration](docs/paseo-integration.md)
- [herdr web ui](docs/herdr-web-ui.md)
- [Session titles in cmux](docs/session-titles.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Changelog](CHANGELOG.md)
- [Security policy](SECURITY.md)
- [Code of conduct](CODE_OF_CONDUCT.md)

## Contributing

Bug reports, focused fixes, portability improvements, and documentation corrections are welcome. Start with [CONTRIBUTING.md](CONTRIBUTING.md) and use the repository issue templates.

For vulnerabilities or credential-handling problems, do not open a public issue. Follow [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE)

Fresh manifest-enabled isolated Codex sessions carry existing canonical hook
approvals across verified equivalent script paths. Changed or unapproved hooks
still require native review; see [hook trust](docs/codex-integration.md#hook-trust-in-isolated-sessions).

## Trusted preservation

See [the trusted preservation guide](docs/trusted-preservation.md) for the
one-shot, native-confirmed Git preservation command, including its strict
opt-in GitHub SSH identity binding for live remote verification.
