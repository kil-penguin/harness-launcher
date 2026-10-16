# Architecture and trust boundaries

## Overview

`harness-launcher` is a shell and executable adapter. It registers a project directory as a short profile command, translates presets into runtime arguments, prepares project-scoped runtime state, and then hands control to the selected CLI.

```text
~/.zshrc
  └─ harness_register <project>
       └─ reads config/launcher.env
            └─ defines <prefix>() and completion
                 ├─ Claude Code
                 ├─ Codex CLI  → prepare .harness/codex
                 └─ Kiro CLI   → prepare .harness/kiro
```

External orchestrators use the same runner through a registered executable profile or `harness-exec` rather than depending on shell startup:

```text
workspace manager
  └─ <profile-prefix> ...
       └─ harness-exec <project> ...
       └─ the same _harness_launcher_run policy path
```

The current workspace is adopted only when its canonical path resolves inside the registered project; explicit `--cwd` uses the same boundary. Workspace managers own UI and worktree lifecycle; the launcher continues to own runtime homes, auth routing, MCP, skills, hooks, presets, and observability.

The launcher is not a model proxy and does not host an API. Gateway modes are optional routes to user-configured external processes.

## Registration

Each project provides:

```bash
# config/launcher.env
HARNESS_NAME="Example harness"
HARNESS_PREFIX="ex"
```

`harness_register` resolves the project to an absolute path, sources `launcher.env`, defines the prefix function, and attaches completion when Zsh's `compdef` is available. `harness-profile register` persists the same prefix as an executable command without copying policy. `harness-auto` uses the Python `harness_profile_resolver.py` to read only regular registry entries, resolve the canonical current directory, and select the single longest owning boundary before delegating to `harness-exec`; it never guesses from a repository name or remote. `--explain` uses the same resolver without launching an agent.

`harness_shell_enable` is an opt-in interactive adapter over that executable
boundary. It keeps its activation flag as an unexported Zsh global, so the
child `harness-exec` shell cannot reactivate the wrapper and recurse. The
launcher-owned `codex` function switches to `harness-auto codex`; a temporary
launcher-owned `claude` function classifies only its first argument. Native
Claude management subcommands use an internal `claude-management` route;
everything else uses `harness-auto claude base`. The management route preserves
argv and the selected project's PWD, loads its local environment, and returns
before model/effort, MCP-argument, observability, title, or session-isolation
mutation. It deliberately does not inject the launcher's Claude session MCP
overlay: `claude mcp list` reports what the native CLI discovers from user and
project configuration. Authentication remains in Claude's native user-global
store rather than becoming profile-local. `harness_shell_disable` removes only
the exact Claude function it installed. Existing aliases/functions are never
overwritten, and `command codex` / `command claude` bypass functions without
modifying `PATH`.

Because `launcher.env` is sourced, registration is a trust decision. The launcher does not attempt to parse or sandbox arbitrary shell code in that file.

Native Codex entrypoints treat `HARNESS_CODEX_GLOBAL_MCP_ALLOWLIST` as trusted launcher configuration, not an ambient user-shell setting. Immediately before each `codex-home-prepare.sh` child, they trim and deduplicate it and either export the resulting nonempty comma-separated list or unset it. This makes consecutive configured, explicitly empty, and absent values independent.

## Command routing

The prefix function and executable profile both delegate through `harness-exec` to `_harness_launcher_run`:

```text
ex                         interactive TUI
ex <mode>                  direct Claude Code
ex fable                   direct Claude Code, fable alias + high effort
ex codex <profile>         native Codex CLI
ex kiro-cli <mode>         native Kiro CLI
ex kiro <mode>             Claude Code through a Kiro gateway
ex codex-gateway <mode>    Claude Code through a Codex gateway
```

Runtime-specific arguments remain arrays until execution. The launcher passes unknown arguments through to the selected CLI.

In shell-auto mode, PWD remains the profile-selection authority. Explicit
Codex `--cd`, `--cd=`, and `-C` values are canonicalized and must exist inside
the selected harness boundary. An outside PWD cannot select a profile by
pointing `--cd` inward, and an inside PWD cannot escape outward through a later
Codex argument. Claude management classification is intentionally first-token
only, so prompt text (`claude 'mcp list'`), `-p doctor`, and tokens after `--`
remain session launches. Option-before-subcommand syntax is not reordered;
`command claude` is the native escape for that form.

## Binary selection

Native Codex uses this precedence:

```text
HARNESS_CODEX_BIN
  → codex resolved from PATH
  → Codex.app bundled CLI only when HARNESS_CODEX_ALLOW_APP_FALLBACK=1
```

Kiro follows the same explicit-override pattern through `HARNESS_KIRO_BIN`, then `kiro-cli` from `PATH`.

This order matters on macOS because an app-bundled CLI or a version-manager shim can differ from the executable expected by the user. Verify binary selection from the same login shell that loads the launcher.

## Generated runtime homes

The launcher keeps project-specific state under:

```text
<project>/.harness/codex
<project>/.harness/kiro
```

These directories can contain generated config, session history, plugin state, hooks, skills, and links to the active runtime auth store. They should be ignored by Git and treated as disposable output.

User-authored sources live outside `.harness`:

```text
config/launcher.env
config/codex-surface.json
.mcp.json
.mcp.local.json
mcp.local.json
mcp.kiro.local.json
.claude/skills/
.codex-only/skills/
.claude/settings.local.json
config/.local/
```

Preparation is idempotent. Re-running a launcher command converges generated files to the current source configuration.

When `config/codex-surface.json` is present, it is the membership boundary for generated Codex skills, imported Claude plugins, Codex-only profiles, and enabled MCP servers. Host tokens are expanded at preparation time. The generated catalog records exact source paths and hashes; it is evidence, not source.

## MCP configuration

Committed MCP definitions can live in `.mcp.json`. Machine-local additions can live in `.mcp.local.json` or `mcp.local.json`.

Those three files are shared by every runtime. `mcp.kiro.local.json` is read only by Kiro preparation, which is the supported way to give one runtime a server the others must not receive; Claude and Codex preparation never read it.

For a local stdio script, use `${HARNESS_ROOT}/core/scripts/example.sh` in `command` or `args` when the path belongs to the registered harness. The launcher resolves this explicit prefix against the canonical harness root, checks that the result is a file inside that root, and passes the resulting absolute path to Claude, Codex, and Kiro. It leaves URLs, credentials, environment fields, and other arguments unchanged. Bare relative script paths for `bash`, `sh`, `zsh`, `python`, and `node` fail before launch with a migration hint. The source JSON remains the definition authority; `.harness/claude/mcp-full.json`, Codex TOML, and Kiro JSON are derived runtime artifacts, so starting in `projects/` or a nested worktree does not change a script's meaning. Isolated Claude sessions put their full/light MCP artifacts in the session state directory outside the Git worktree and write them owner-only (0600), keeping local definitions and credentials out of submission. This root-path rule adds no always-on policy clause; it replaces the previous runtime-dependent relative-path interpretation.

Runtimes differ in how an authenticated HTTP MCP server reaches its endpoint. Claude expands `${VAR}` in headers itself, Codex preparation translates an `Authorization: Bearer ${VAR}` header into `bearer_token_env_var`, and Kiro CLI supports neither: its agent schema takes only `url` into account for http transport, so a header-authenticated server falls into OAuth discovery and fails. Kiro preparation therefore rewrites such a server into an `mcp-remote@latest` stdio bridge carrying the same headers, leaving `${VAR}` unresolved so the bridge substitutes it from the inherited environment. HTTP servers without headers, and servers already using stdio, are untouched. The SSH-tunnel light filter runs before translation, so a loopback tunnel server is still dropped rather than bridged.

The launcher rejects duplicate server names across these files. Local config extends committed config; it does not override it silently. Native Kiro validates and renders MCP configuration in external staging before it materializes a runtime home: a duplicate leaves an existing `.harness/kiro` unchanged and creates no `.harness/kiro` state for a fresh project.

Codex preparation translates supported MCP entries into TOML:

```json
{
  "mcpServers": {
    "docs": {
      "command": "npx",
      "args": ["-y", "@example/docs-mcp"]
    }
  }
}
```

```toml
[mcp_servers.docs]
command = "npx"
args = ["-y", "@example/docs-mcp"]
```

HTTP bearer values should be represented through environment-variable references. Generated config stores the variable name, not the secret value.

An exact Codex surface can additionally declare `codex-global-allowlist` as an MCP definition source. That source projects only names selected by `HARNESS_CODEX_GLOBAL_MCP_ALLOWLIST` from `$HOME/.codex/config.toml`; the global file is the definition authority, while the exact profile remains the enablement authority. The launcher rejects duplicate names across every selected source instead of selecting one implicitly. Static `env` and `http_headers` fields are rejected: use `env_vars`, `env_http_headers`, or `bearer_token_env_var` references so generated project state never copies secret values.

## Global state and concurrency

Most Codex state is project-scoped, but bundled plugin caches and the Chrome native-host bridge can use global `~/.codex` paths. These writes are a cross-project critical section.

On macOS, preparation opens a persistent lock file and acquires `/usr/bin/lockf` on an inherited descriptor. The kernel holds the lock for the protected subshell lifetime, including signal and child-process cases. The lock file can remain on disk after release; successful reacquisition proves that no process still owns it.

Do not replace this with PID files, mtime-based stale reclamation, or signal cleanup that removes a directory while child work continues.

### Isolated harness sessions

The opt-in isolated-session canary separates three paths:

- `HARNESS_SOURCE_ROOT` owns the canonical Git directory, product checkouts, and machine-local configuration.
- `HARNESS_SESSION_ROOT` is a detached, per-UUID repository with a distinct Git directory and no remote, containing the session's tracked harness state and private index.
- `HARNESS_RUN_DIR` is the runtime working directory: the session root for harness work, or the original product worktree selected with `--cwd`.

For leading explicit isolation controls, `harness-exec` does not adopt the caller's
directory. Both `--isolated` and `--isolated-session <uuid>` leave the working-directory
default to the isolated session, including a precreated UUID opening the launcher TUI.
An explicit `--cwd` is still validated against the original registered project
before isolation is acquired.

Machine-local MCP files are referenced from the canonical source and excluded
from submissions. `config/.local` is read from the source boundary and is never
copied. A heartbeat keeps live `OPEN` sessions from being declared abandoned;
the UUID workspace remains recoverable after an unclean exit.

#### Claude plugins in isolated sessions

Claude Code applies a `project`- or `local`-scope plugin install record only
when its `projectPath` is the current project root or shares its canonical Git
root. The session root is a separate clone, so without help a plugin installed
with `claude plugin install <id> --scope project` for the canonical root reports
`isn't installed` there (plugins whose marketplace source is a relative path
are read in place and are unaffected). Before an interactive isolated session
starts Claude Code, through any Claude route including gateways and the TUI,
`bin/claude-plugin-scope-mirror.py` copies each such record whose `projectPath`
is the canonical root into one for the session root and removes records for
session roots under the state directory that no longer exist. Plugin identity
stays `<name>@<marketplace>`, so tool names and `permissions.ask` rules match.

This makes the launcher a writer of Claude Code's user registry
(`installed_plugins.json` in the plugins directory Claude Code resolves from
`CLAUDE_CODE_PLUGIN_CACHE_DIR`, `CLAUDE_CONFIG_DIR` and
`CLAUDE_CODE_USE_COWORK_PLUGINS`). It writes only when a change is needed,
under `<registry>.harness-launcher.lock`, compares the bytes immediately
before an atomic replace, and verifies the result; a symlinked, hard-linked,
malformed or non-version-2 registry is never rewritten. Every failure prints a
warning and the launch continues. Residual risks: Claude Code does not take
the launcher lock, so a Claude Code write that lands between the launcher's
final comparison and its replace is lost (two Claude Code processes already
race the same way on the file path; under Claude Code's optional storage
backend, which has its own lock, this is a new but equally short window). A
Claude process starting at the same moment can also write back an older
snapshot, which leaves the plugin unloaded in that session until the next
launch or continuation. Headless runs are excluded because they withhold
harness-local secrets from unattended agents. `HARNESS_CLAUDE_PLUGIN_MIRROR=0`
in the launching environment (not in `settings.local.json`) disables the
mirror.

Profiles may default only fresh interactive direct-Claude and native-Codex
routes into this boundary. A bare interactive profile command chooses an
isolated workspace or the existing shared workspace before runtime menus,
preparation, or session collection run. The router classifies arguments before
allocating a UUID: non-interactive, batch, help, diagnostics, and gateway/Kiro
routes remain legacy (an explicit `--isolated` still isolates them and runs in
the session root), while generic continuation commands require either an
exact `--isolated-session <uuid>` or an intentional `--no-isolated` rollback.
Fresh interactive sessions, including precreated UUIDs, skip foreground retention
collection. Explicit `--isolated`, `--isolated-session`, and `--no-isolated`
selectors bypass the workspace question. A source subdirectory maps to the
same relative path in the clone; the broker-created `projects` link retains its
source workspace. The broker validates the session journal, recorded source,
state location, and root inode before mapping and again at TUI entry. Other
symlink escapes and missing clone directories fail closed.

Argument classification stops at the prompt/freeform boundary so prompt text
that resembles a launcher switch cannot alter isolation.

Each lease-v1 session has a per-UUID `runtime.lock`. The launcher acquires it
before resume and holds it for the complete runtime; its close-on-exec descriptor
prevents runtime, heartbeat, and title-broker children from extending ownership.
`OPEN` can resume only while that launcher lease is held. `ABANDONED`,
`CONFLICT`, and retained `CLOSED` records reopen the same UUID; `SUBMITTED` and
`INTEGRATING` require delivery recovery; `DELIVERED` and `DISCARDED` are
permanently terminal (`resume` and `recover` refuse them).

`harness-session discard <uuid>` retires an `ABANDONED` or `CONFLICT` session
whose work will not be delivered (for example, work another session already
delivered). It runs under the global integration lock and takes the session's
runtime lease without waiting, as garbage collection does; a held lease
refuses. Before any state change it writes `discarded.patch` into the record:
the binary diff of the work tree against `base-sha`, staged through a
temporary copy of the index that submit would stage (the session's own index
for interactive sessions, the `trusted.git` index for headless ones) with the
session's broker git, plus `discarded-at` (UTC). The patch holds the work
tree's tracked content as that index tracks it plus untracked, non-ignored
files: force-added ignored files stay, files removed with `git rm --cached`
and now ignored stay deleted, and other gitignored files and the excluded
machine-local paths are left out. Neither the session index nor a headless
session's `.git` is written. The diff format is fixed (`--no-color
--no-ext-diff --no-textconv --src-prefix=a/ --dst-prefix=b/`), so user diff
settings cannot change the patch. Completeness rests on the exit status of
every step (index copy, `add`, `diff`) and on the rename that publishes the
patch only after git wrote it; the patch, `discarded-at` and the record
directory are flushed with `F_FULLFSYNC` (plain `fsync` where that fails, which
does not flush the drive cache) before the journal changes. Any failure, or an
interrupt (the discard's temporary files are removed), refuses and leaves
the journal unchanged; an interrupt after the patch was published can leave
`discarded.patch` and `discarded-at` in the `ABANDONED` or `CONFLICT`
record, which the next `discard` or reopen replaces. Only then does the
journal become `DISCARDED`, keeping the
identity a `CONFLICT` carried. `DISCARDED` is reachable only through
`discard`: `harness-session transition <uuid> DISCARDED` refuses, and every
reopen to `OPEN` removes a leftover `discarded.patch` and `discarded-at`.
`OPEN` (use `exit` or `close`), `SUBMITTED` and `INTEGRATING` (possibly
indeterminate; use `recover`), `CLOSED`, `DELIVERED` and `DISCARDED` refuse
with exit 2.

State transitions: `OPEN` → `SUBMITTED` | `CLOSED` | `ABANDONED`;
`SUBMITTED` ⇄ `INTEGRATING`; `INTEGRATING` → `DELIVERED` | `CONFLICT`;
`ABANDONED`, `CONFLICT`, `CLOSED` → `OPEN`; `ABANDONED`, `CONFLICT` →
`DISCARDED`.

Serialized garbage collection retains nonterminal, malformed, legacy, future-
dated, leased, and within-grace records. For an expired `CLOSED`, `DELIVERED`
or `DISCARDED` record (`DISCARDED` only with its `discarded.patch`), it
rereads state while holding the runtime lease, validates that the workspace is the canonical state directory's exact direct child, renames it to
a same-parent tombstone, and removes only that tombstone. The durable journal is
retained. The default grace is 24 hours and the accepted range is 0–7 days.

Native Codex data has a separate lifetime. For Native-bearing roots GC first
creates a private immutable snapshot in `<state>/native-history`, masks the
original root in recoverable `.archiving-*` staging, then verifies a derived
restore through the characterized Native binary. Original and other Native
homes are unreadable to that probe. All raw rollout bytes and paginated items
must match; unknown artifacts, live writers or failed verification retain the
root. Old Native `.retired-*` tombs stay recoverable. Archives and completed
journals are outside the workspace GC target and are never automatically pruned.
See [Durable native Codex history](codex-history.md).

`harness-session report --json` observes retention metadata without invoking GC,
archive, history verification, or hold cleanup. Optional `--sizes` traversal is
bounded and metadata-only. It always reports `delete_safe: false`; see
[session disk diagnostics](session-report.md) for unknown states and size semantics.

Delivery has one kernel-locked lane per host. The session submits a base SHA,
binary patch, NUL-delimited path/mode/blob manifest, canonical source, and
canonical remote bound by one digest and rechecked before and after verification.
The broker clones the current remote tip, requires the repository-owned
auto-delivery verifier on the staged candidate, applies the patch three-way,
and performs a non-force push. Remote movement before that push discards the
candidate and reruns apply plus verification. The successful push is the remote
compare-and-swap acknowledgement; a fresh clone must then contain the delivered
SHA as an ancestor and reproduce its exact path/mode/blob manifest before the
journal can become `DELIVERED`. A later remote descendant therefore cannot turn
an accepted delivery into a false conflict. Independent hosts rely on the remote
non-fast-forward comparison; they never bypass it with force push. A crash in
`INTEGRATING`, including between remote acceptance and the local acknowledgement,
reconciles a proven pushed candidate to `DELIVERED` or returns an unpushed
submission to `SUBMITTED`. Clean close or normal exit records terminal `CLOSED`.

Manifest-enabled homes also keep an atomic successful-input fingerprint plus a source-identity watch snapshot. The lean warm path validates watched file identities, semantic TOML policy, launcher-owned output hashes, product-plugin skill digests, explicit-only policies, skill/plugin directory topology, every managed skill link, and the normalized global-MCP definition digest before returning; it does not rescan plugin tests/docs/assets. Changing selected global definitions or allowlist membership therefore invalidates the warm path. Unexpected generated-home skill routes force a cold rebuild and reversible quarantine, and marker membership alone never proves ownership. Auth contents, sessions, hook trust state, and generated output mtimes remain runtime state and do not invalidate source generation. A cold rebuild leaves the live success stamp in place while it prepares a candidate transaction, then publishes the replacement success stamp last.

### Headless isolated runs

`harness-headless <profile> --prompt-file F --result-file R --lock-file L --budget-usd N --timeout-min M [--settings-file S] [--model X] [--effort E] [--agent claude|codex] [--model-endpoint U --endpoint-key-file K [--max-model-requests Q]] [--target T --target-digest D --task-id I --approval-sha A]`
runs one unattended Claude (default) or Codex task for a registered profile.
It is the only non-interactive route that works inside an isolated session.
Interactive and legacy routes do not change, and a call without the new
options behaves exactly as before.

- **Agent options.** `--agent` is `claude` (default) or `codex`. `--effort` is
  `low`, `medium`, `high` or `xhigh` for Claude (Claude Code's `effortLevel`
  values; `max` is session-only) and `minimal`, `low`, `medium`, `high` or
  `xhigh` for Codex. `--model-endpoint`, `--endpoint-key-file` and
  `--max-model-requests` are Codex-only; Codex also needs `--model` (a plain
  name) and refuses `--settings-file`, which holds Claude settings. Every
  invalid combination is `refused` with a result file, before anything starts.

- **Lock and lifetime.** It holds an exclusive `flock` on `L` from start until
  the result file is in place, including delivery, and stays in the
  foreground. A held lock exits 75 and leaves `R` alone; any other lock error
  exits 2 with a `refused` result when `R` is writable. `SIGTERM`, `SIGINT` and
  `SIGHUP` kill the run's process group, write a `failed` result (`exit_code`
  128 + signal), and only then release the lock.
- **Environment.** Only `HOME`, `PATH`, `USER`, `LOGNAME`, `SHELL`, `LANG` and
  `LC_*` pass through, plus `HARNESS_SESSION_STATE_HOME` and
  `XDG_STATE_HOME` so the launcher finds the same session state. It adds
  `HARNESS_HEADLESS=1`, `HARNESS_PYTHON_BIN` (the resolved interpreter),
  `GIT_TERMINAL_PROMPT=0`, `HARNESS_COMMIT_MESSAGE_FILE` (see **Commit
  message**), and `TMPDIR` and `CLAUDE_CODE_TMPDIR` both set to a
  fresh `/private/tmp/hh-*` directory (0700, owned by the user, short enough
  for Claude's per-uid socket directory), so the shared
  `/private/tmp/claude-<uid>` can stay write-denied. That directory is
  removed before delivery and on every exit path. With `HARNESS_HEADLESS=1` the launcher does not
  export the harness `.claude/settings.local.json` `env` block, does not derive
  a per-harness `GH_TOKEN`, and passes no `--mcp-config`.
- **Session.** It calls `harness-exec <harness> --isolated --passthrough ...`,
  so the launcher creates the journal and lease itself and the run directory
  is the session root. The headless clone uses `git clone --no-hardlinks`,
  copies `.mcp.local.json`, `mcp.local.json` and `.claude/settings.local.json`
  (without its `env` block) instead of linking them, never links `projects/`,
  and refuses a symlink on any of those paths. Before the agent starts it
  writes a `headless` marker and a launcher-owned bare git dir
  (`trusted.git`, the base commit and a matching index) into the session
  record, outside every sandbox write path.
- **Claude.** `claude -p --output-format json --max-budget-usd N
  --permission-mode acceptEdits --strict-mcp-config [--model X]`, with the
  prompt on stdin, in its own process group without a controlling terminal.
  The launcher merges every `--settings` into one (dicts merge, lists are
  unions, the last scalar wins): its launch-record settings, the session
  settings, the caller's `S`, and the mandatory containment, passed last.
  - `disableAllHooks: true` (project hooks run outside the sandbox) and
    `disableBypassPermissionsMode: "disable"`. Hooks are off, so headless runs
    write no launch record; a later `resume` of such a Claude session gets the
    default permissions.
  - `sandbox`: `enabled`, `failIfUnavailable`, `allowUnsandboxedCommands:
    false`, `autoAllowBashIfSandboxed`, `network.strictAllowlist` with an empty
    `network.allowedDomains`; `filesystem.denyRead` for `~/.hermes`, `~/buzz`,
    `~/.ssh`, `~/.config/gh`, `~/.aws`, `~/.claude`, the source root and the
    caller's paths; `filesystem.denyWrite` for `/private/tmp/claude-<uid>`,
    `/tmp/claude-<uid>` and the session `.git/config`, `.git/hooks` and
    `.git/info`; `filesystem.allowWrite` for the run's temp directory.
  - `permissions.deny`: `Read` on every `denyRead` path; `Edit`, `Write` and
    `NotebookEdit` on those paths, `~/Library/LaunchAgents`, the temp
    directories and the session git config, hooks and info; `WebFetch`;
    `WebSearch`; Bash rules matching `harness-session`, `session-isolation.sh`,
    `auto-deliver`, `git push`, `sudo` and `launchctl` anywhere in the
    command; and `hermes`, `buzz` and `rm -rf` as prefixes.
  - `S` may contain only `_note`, `permissions.deny`,
    `sandbox.filesystem.denyRead` and `sandbox.network.allowedDomains` (string
    lists, added to the mandatory ones). Any other key is `refused`.
  - `--effort E` becomes `effortLevel: E` in the mandatory block, so no
    caller setting overrides it; `xhigh` also sets `alwaysThinkingEnabled:
    true` (the API rejects `xhigh` without thinking, as the launcher's own
    `rich` mode handles). Without `--effort` neither key is set.
- **Codex.** `--agent codex` keeps the lock, environment allowlist, temp
  directory, commit-message file, timeout, lingering-process kill, broker,
  verifier and result writing above; only the agent process differs.
  - *Session.* `harness-headless` runs the launcher's own steps itself:
    `harness-session gc`, `session-isolation.sh create` with
    `HARNESS_HEADLESS=1` (the same headless clone and `trusted.git`), the
    `runtime.lock` lease (`fcntl` lock, as the launcher's `zsystem flock`), the
    `harness-launcher: isolated session <uuid>` line in `R.log`, a heartbeat
    every 30 s, and `exit` after Codex ends and lingering processes are
    killed. A lease failure right after `create` runs `exit` at once, so no
    unleased `OPEN` session is left. It never runs `harness-exec`,
    `harness-codex`, `codex-home-prepare.sh` or anything under
    `.harness/codex`, which write to the canonical checkout.
  - *Binary.* The launcher's `harness_codex_bin_resolve`
    (`HARNESS_CODEX_BIN`, then `PATH`), with symlinks followed, and the
    `codex-code-mode-host` beside it, which must be an executable regular file;
    both must be owned by the user or root with no group or other write (else
    `refused`). Code-mode models such as gpt-6.1-sol run every tool
    through that host; with code mode off they have no tool at all (measured
    live: "code-mode host is disabled"). The npm vendor build ships both
    (`.../@openai/codex-darwin-arm64/vendor/aarch64-apple-darwin/bin/`); a
    standalone `codex` may not, so point `HARNESS_CODEX_BIN` at the vendor
    binary, not at a `mise` shim or the npm `codex.js` wrapper. The sandbox
    allows exactly these two files.
  - *CODEX_HOME.* A fresh 0700 `<temp dir>/codex-home` with exactly two
    launcher-written 0600 files. `config.toml`: `model`, `model_provider =
    "loop"`, `model_reasoning_effort` (only with `--effort`), `approval_policy
    = "never"`, `sandbox_mode = "danger-full-access"` (Codex's own Seatbelt
    cannot nest under the launcher's), `check_for_update_on_startup = false`,
    `[model_providers.loop]` with `base_url` the forwarder, `wire_api =
    "responses"`, `supports_websockets = false`, `requires_openai_auth =
    false`, `stream_idle_timeout_ms = 300000`, no `env_key`, and
    `http_headers` holding only the run's forwarder token
    (`X-Harness-Forwarder-Token`, 128 random bits), and `[history]
    persistence = "none"`; no MCP servers, hooks, notify, profiles or project
    trust entries (so a repository `.codex/` config is not loaded).
    `AGENTS.md`: the session clone's `.claude/rules/*.md` except `_index.md`,
    concatenated as `codex-home-prepare.sh`'s fallback does (`## <name>` and
    the file), read outside the sandbox before Codex starts; a symlinked rule
    or rules directory is `refused`. The clone's own root `AGENTS.md` still
    applies as Codex's project instructions. Codex writes its own state into
    `CODEX_HOME`, so the directory stays writable; the sandbox cannot rewrite,
    replace or rename the two files or move the directory, but it can add
    other files there, which only a Codex the agent starts itself would read,
    inside the same sandbox. The running Codex has already read its
    configuration.
  - *Command.* `sandbox-exec -f <temp dir>/codex.sb <codex> exec --json -o
    <temp dir>/last-message.md -C <session root> -`, in the session root, in its
    own process group, the prompt and delivery note on stdin. The environment
    is the allowlist above plus `CODEX_HOME`, without `CLAUDE_CODE_TMPDIR`.
  - *Forwarder.* A thread of `harness-headless`, outside the sandbox, the only
    process that ever holds the key. It listens on `127.0.0.1`, random port,
    HTTP/1.0 with one request per connection, and also binds `::1` at the same
    port without listening, so no other service can be reached there. At most
    8 requests run at once (503 beyond that), and a client silent for 60 s is
    dropped. It answers 403 unless `Host` is exactly `127.0.0.1:<port>`,
    there is no `Origin` header (DNS rebinding, browsers) and the one
    `X-Harness-Forwarder-Token` header matches the run's token (so no other
    local process can use it), and 404 for anything but `POST /v1/responses`
    and `GET /v1/models` (exact paths, no query). A `POST` needs one
    `Content-Length` of at most 32 MiB (411, 413) and a JSON object body with
    no duplicate key anywhere (400: a parser upstream could read the other
    `model`) whose `model` is `--model` (403). It counts accepted `POST`s;
    past `--max-model-requests` (default 400) it answers 403 and the run
    becomes `budget`. Upstream it sends only allowlisted request headers:
    `Accept`, `Content-Type`, `User-Agent`, `OpenAI-Beta`, `originator`,
    `version`, `session_id`/`session-id`, `conversation_id`/`conversation-id`,
    `thread-id`, `x-client-request-id`,
    `x-openai-internal-codex-responses-lite` and `x-codex-*` (what Codex 0.160
    sends, checked by the real-binary test), minus any named in `Connection`;
    it sets `Host` and `Content-Length` and adds `Authorization: Bearer
    <key>`. It talks to the validated endpoint only; a 3xx becomes 502 and is
    never followed. The answer is relayed chunk by chunk without buffering
    (SSE), with an upstream read timeout of 330 s (Codex's stream idle timeout
    plus 30 s); when Codex disconnects, the upstream connection closes. It
    never logs a request line, header or body, and a handler error closes the
    connection without a traceback.
  - *Key file.* `--endpoint-key-file` must be a regular file opened without
    following a symlink, with one link, owned by the current user, mode 0600,
    holding one printable token. Only the launcher reads it; the key never
    appears in argv, the environment, `config.toml`, `R.log` or the result,
    and a refusal never quotes it. The production place is
    `~/.config/harness-launcher/cliproxy-headless.key`, which the sandbox
    denies.
  - *Endpoint.* `--model-endpoint` must be exactly `http://127.0.0.1:<port>/v1`.
  - *Sandbox.* The Seatbelt profile (`harness-session codex-sandbox-profile`)
    is the only boundary around the whole Codex process tree. It is the
    verifier profile below, with the session root and the temp directory as
    its copy and temp dir and the resolved Codex binary as its trusted path:
    deny by default; exec and read of system and toolchain trees, `PATH`
    directories, the session root, the temp directory (so `CODEX_HOME`) and
    the binary; writes only to the session root, the temp directory and a few
    devices; unix sockets only inside the temp directory; signals only within
    the sandbox, no `kern.proc` sysctl, and of `/dev` only the fixed device
    list. Changed for Codex:
    - PTYs as Codex's own Seatbelt policy grants them (its unified exec runs
      commands in one): `(allow pseudo-tty)`, `/dev/ptmx`, and a
      `/dev/ttys*` slave only with the `com.apple.sandbox.pty` extension the
      kernel issues for a PTY opened inside the sandbox. A terminal the user
      has open is never readable, writable or ioctl-able (Codex's own policy
      also allows ioctl on any slave; this one does not).
    - Read and exec of exactly the code-mode host beside the binary.
    - Network: TCP to exactly `localhost:<forwarder port>`. Seatbelt only
      accepts `*` or `localhost` as a host, so loopback at that one port is
      the narrowest rule it can express; no other port, no `localhost:*`.
    - Placed after every allow: denies for `~/.ssh`, `~/.hermes`, `~/buzz`,
      `~/.codex`, `~/.claude`, `~/.config/gh`, `~/.config/harness-launcher`,
      `~/.aws`, `~/Library/Keychains`, `/Library/Keychains`,
      `~/.git-credentials`, `~/.netrc`, `~/.config/git` and the source
      checkout (tests put each of `.ssh`, `.codex`, `.claude`, `.config/gh`,
      `.hermes` and `.config/harness-launcher` on `PATH`, which the profile
      reads, and the deny still holds), the state home except the session
      root (other sessions' records and worktrees), and writes to
      `CODEX_HOME` itself and its two files.

    Login-shell dotfiles and Codex's skill folders under `HOME` are
    unreadable; the real-binary test shows Codex 0.160 runs its `zsh -lc`
    commands and `apply_patch` edits regardless.
  - *Preflight.* Each of these is `refused` before the first model request:
    no Codex binary; a key file that fails the checks above; a forwarder that
    does not start; `GET /v1/models` through the forwarder not answering 200;
    `sandbox-exec -f <profile> <codex> --version` or
    `codex-code-mode-host --help` under the profile failing; and the key file
    being readable inside the sandbox (`/bin/cat` under the profile). The last
    two run after the session clone exists, so that session is finished
    (`CLOSED`) on refusal.
  - *Signals.* `SIGTERM`, `SIGINT` and `SIGHUP` kill the process group and
    then every process left under the session root or temp directory (one
    `lsof` snapshot, so also one that called `setsid`), finish the session and
    release the lease before the `failed` result is written. The Claude path
    does the same.
  - *Result.* `summary` is the `-o` file (read without following a symlink,
    at most 3000 characters), `cost_usd` is null (a subscription),
    `num_turns` counts `turn.completed` events (null without any),
    `transcript` is null, and `usage: {"input_tokens", "output_tokens"}` sums
    those events' usage when there is at least one. Status: `timeout`; else
    `budget` when the request cap was hit; else `failed` for a nonzero Codex
    exit (summary the `-o` file or `codex exited N`); else delivery as for
    Claude.
- **Commit message.** The broker delivers the work tree as one commit; the
  agent's own commits are not kept as commits. `harness-headless` appends a
  delivery note to the prompt asking the agent to write that commit's message
  to `$HARNESS_COMMIT_MESSAGE_FILE`, `<run temp dir>/commit-message`, which
  the sandbox lets Bash write. After Claude exits and lingering processes are
  killed, and before the temp directory is removed, it reads the file without
  following a symlink or blocking (`O_NOFOLLOW|O_NONBLOCK`), only if it is a
  regular file with a single link, of at most 8192 bytes of strict UTF-8. It
  turns CRLF, CR, U+2028 and U+2029 into LF, removes every other control
  character (C0, C1, DEL; NUL and ESC included) except TAB
  (so ESC and terminal sequences cannot reach a terminal that shows `git
  log`) and every Unicode format character (bidi overrides, zero-width), removes
  CI-skip directives (`[skip ci]`, `[ci skip]`, `[no ci]`, `[skip actions]`,
  `[actions skip]`, case-insensitive) so the
  delivered commit cannot switch off the repository's checks, strips trailing
  whitespace, drops leading and trailing blank lines and any trailer line whose
  key is `Harness-Session` or `skip-checks` when compared loosely (NFKC, so
  full-width letters and colons fold; case-insensitive; spaces, hyphens and
  underscores in the key ignored; leading whitespace allowed). Other trailers and
  GitHub keywords such as `Fixes #12` pass through unchanged. The message is
  used only if the subject is non-empty and at most 100 characters and there
  are at most 200 lines. It is written atomically (0600) to the
  launcher-owned record, `<record>/commit-message`, which the agent sandbox
  cannot write, only after the broker has found the session `ABANDONED` with
  changes and recovered it, right before `close`. Every outcome other than
  `delivered` leaves no `commit-message` in the record (timeout, budget,
  failed, refused, `no_changes`, `conflict`, and refusals or failures inside
  delivery remove or never write it; only a SIGKILL of `harness-headless`
  during `close` can leave it behind), so a session resumed and closed later
  by its owner gets the generic message. A
  `printf` or `echo` command whose text trips a Bash deny rule (it contains
  `harness-session`, `git push` or another denied word) is refused by the
  sandbox, so such a message is never written and the generic one is used.
  The broker commits with that message, a blank line and `Harness-Session:
  <uuid>` (`git commit --cleanup=whitespace -F`, same `harness-broker`
  identity) when the record holds a regular, non-symlink `commit-message`, in
  headless and interactive sessions alike; otherwise with `harness session
  <uuid>`. Anything else (no file, a symlink, a FIFO, too large, invalid
  UTF-8, an invalid subject, an I/O error) falls back to the generic message.
  For a delivery, the run log names the source used (`harness-headless: commit message:
  agent` or `generic (<reason>)`); the result file does not change. The
  agent's `.git` is never read for this.
- **Timeout.** After `M` minutes (fractions allowed) the whole process group
  gets `SIGTERM`, then `SIGKILL`. Status `timeout`, `exit_code` 124. The
  session is left for its owner.
- **Delivery.** After Claude exits, `harness-headless` kills its process
  group and then every remaining process of the user whose cwd or open file
  is inside the session root or the run's temp directory (one `lsof`
  snapshot), and removes that temp directory. Only then does the broker run,
  with the caller's `TMPDIR` and no `CLAUDE_CODE_TMPDIR`, so broker git and
  the repository verifier never write or execute files where the sandboxed
  agent could write. Broker git on a headless
  session (`exit`, `submit`, `close`; decided only by the marker) never reads
  the session's `.git`: it runs `git --git-dir=<record>/trusted.git
  --work-tree=<session root>` with `GIT_CONFIG_NOSYSTEM=1`,
  `GIT_CONFIG_GLOBAL=/dev/null`, `core.fsmonitor=false`,
  `core.hooksPath=/dev/null`, `submodule.recurse=false`, an empty
  `diff.external` and `core.untrackedCache=false`. The session's config,
  hooks, modules and alternates are never read, and a work-tree
  `.gitattributes` names filter or diff drivers that are not defined, so they
  are no-ops. The work tree holds the final content whether or not the agent
  committed, so `add -A -- .` captures it all; the excluded paths
  (`config/.local`, `projects`, `.mcp.local.json`, `mcp.local.json`,
  `.claude/settings.local.json`) are listed in the trusted git dir's
  `info/exclude` and reset to the base afterwards, so neither a new file nor
  an edit to a tracked file there is delivered. `add` never names them: an
  exclude pathspec naming a gitignored file makes `git add -A` exit 1. The
  reset and the diff pathspecs match case-insensitively, because the agent
  can negate ignore rules in `.gitignore` and write `Projects/` or
  `config/.LOCAL/`. Git folds only ASCII case, while APFS also folds Unicode
  (`projectſ` with U+017F is `projects` on disk), so every staged path is
  also checked against probe trees of the excluded entries on the session
  volume and on the source checkout's volume (they differ when the state
  home is elsewhere); a path the filesystem resolves to an excluded entry is reset (or,
  for an interactive commit, refused) the same way. Names the filesystem
  keeps distinct, such as `projects.` or `projects` with a trailing space,
  are ordinary paths, and a symlink into `projects/` is delivered as the
  link itself. A broker git error is never read as an answer: a failed
  listing is not "nothing to exclude", a failed change check is not "no
  changes" (the session is kept, not closed), and a failed tree listing is
  not a deletion. Each stops the close before anything is pushed.
  Without global config, global excludes (for example a global `.DS_Store`
  ignore) do not apply; the repository `.gitignore` does. A marker without
  its `trusted.git` refuses (exit 6, status `refused`), and so does a source
  checkout that has moved or been deleted (exit 8, status `refused`; the
  session is kept). A changed session goes
  to `ABANDONED`, a clean one to `CLOSED`. A changed session is recovered and
  closed through the broker. A journal still `INTEGRATING` after close is
  recovered once. `DELIVERED` gives `delivered` with the `delivered-sha`
  readback commit; close exit 3 or 5 gives `conflict` (session kept); exit 9,
  the repository verifier rejected the candidate, gives `failed` (session
  kept); anything else is `failed`.
- **Verifier sandbox.** The repository verifier (`core/bin/auto-deliver.sh`
  from the trusted baseline, run `--staged-only --dry-run --no-push`) runs the
  candidate's tests, which the agent may have written. For headless sessions
  the broker runs it under `/usr/bin/sandbox-exec` with a generated Seatbelt
  profile, on a throwaway copy of the candidate that the broker deletes and
  never reads back; the broker commits and pushes from the candidate itself
  (no hooks, fsmonitor or external drivers), so nothing a test writes into
  the copy's `.git` or work tree runs or is delivered. The profile denies by
  default and allows only: fork, and exec of binaries under `/usr`, `/bin`,
  `/sbin`, `/System`, `/Library/Developer`, the Homebrew prefix, mise and
  other `PATH` directories (never `/` or a directory holding `HOME` or the
  state home), the copy, the temp dir and the trusted verifier; reads of
  those trees plus `/Library/Developer`, `/Library/Frameworks`,
  `/Library/Apple` and `/Library/Perl` (not Application Support,
  Preferences, Logs or Keychains), `/private/var/db/timezone`,
  `/private/var/select`, of `/dev` only the directory listing itself (for
  `ttyname`), `null`, `zero`, `random`, `urandom`, `tty`, `dtracehelper` and
  `/dev/fd` (never a `/dev/ttys*` terminal the user has open), and a few
  `/private/etc` files (`hosts`,
  `passwd`, `group`, `localtime`, `services`, `protocols`, `shells`, `ssl`),
  with the Homebrew `etc` and `var` denied except its OpenSSL and CA config;
  file metadata anywhere (path resolution: `stat`, not directory listings);
  `sysctl` reads except the process table (`kern.proc`); no network at all,
  except unix sockets inside the verifier's temp dir (no loopback services,
  no Docker socket, no `PF_SYSTEM` kernel-control sockets); writes only
  to the copy, a fresh per-verify temp dir outside `HOME` (`TMPDIR` under
  `/private/tmp`, removed afterwards), `/dev/null`, `/dev/tty`,
  `/dev/dtracehelper` and `/dev/fd`. The launcher runs the broker in a new
  session, so the verifier has no controlling terminal and `/dev/tty` never
  reaches the operator's. Process inspection ends with an
  explicit `(deny process-info*)` and then `(allow process-info* (target
  same-sandbox))`: the same-sandbox allow alone did not stop
  `KERN_PROCARGS2` or `KERN_PROC_PID` on outside processes on macOS 26 (the
  broker's and launcher's argv and environment were readable), even with no
  `sysctl-read` allowed. The state home (other sessions'
  worktrees, records and trusted git dirs) is denied even when a `PATH`
  directory holds it, with only the copy and the trusted verifier inside it
  allowed again. Nothing under `HOME` or the state home is
  readable except the copy, the temp dir, the trusted verifier and toolchains
  (mise's installs, cache and `~/.config/mise/config.toml`, `PATH`
  directories), and never `~/.ssh`, `~/.hermes`, `~/buzz`, `~/.config/gh`,
  `~/.aws`, `~/.claude`, `~/Library/Keychains`, `~/.git-credentials`,
  `~/.netrc`, `~/.config/git` or the source checkout.
  User git config is not readable; the verifier gets a generated global git
  config (`GIT_CONFIG_GLOBAL`, identity only), and neither the copy nor the
  trusted clone keeps a remote, so no remote URL with credentials is
  readable. No mach services
  except user and group lookup and logging (so no keychain, launchd job
  submission, LaunchServices `open`, XPC services or AppleEvents); signals
  and process inspection only within the sandbox. A nested `sandbox-exec`
  fails. The environment is `HOME`, `PATH`, `LANG`, `LC_*`, `TMPDIR` and the
  verifier's own switches plus `GIT_CONFIG_GLOBAL`; agent sockets, askpass
  helpers, `GH_*`, `GITHUB_*`, `ANTHROPIC_*`, `OPENAI_*`, `CLAUDE_*` and
  tokens are not passed. Interactive sessions run the verifier as before.
- **Result.** Written atomically (temp file and rename) to `R`, always with
  `"version": 1`:
  `{"version":1,"status":"delivered|no_changes|conflict|failed|timeout|budget|refused","session_id":<launcher UUID|null>,"commit":<sha|null>,"cost_usd":<float|null>,"num_turns":<int|null>,"summary":<Claude result or Codex -o file, at most 3000 chars>,"transcript":<path|null>,"exit_code":<int>,"started_at":<epoch>,"ended_at":<epoch>}`,
  plus the optional `"usage":{"input_tokens":<int>,"output_tokens":<int>}`
  for Codex runs that reported usage.
  `budget` is Claude subtype `error_max_budget_usd` or the Codex request cap;
  `failed` covers other Claude or Codex errors, a missing Claude result,
  signals and delivery failures.
  `refused` (`exit_code` 2 before launch) means the run could not start
  without input or broke containment: an unknown profile, a host
  without `/usr/bin/lockf` (delivery could never lock), a host without
  `/usr/bin/sandbox-exec` or where the verifier profile does not load (checked
  with `harness-session sandbox-check`), an empty or
  unreadable prompt, a non-positive budget or timeout, a settings key outside
  the allowed set, an invalid agent option, a failed Codex preflight, a
  refused headless clone, or a headless record without its trusted git dir.
  Launcher and Claude stderr go to `R.log`. The command exits 0 whenever `R`
  was written and nonzero otherwise.

Bash deny rules match command text and are guardrails only; containment
comes from the mandatory sandbox. User and project settings files still merge
under the flag settings.

Headless Codex threat model. The agent and everything it starts run as the
user under one deny-default Seatbelt profile, with Codex's own approvals and
sandbox off; the profile is the boundary. Inside it the agent can read
toolchains, the session clone and the run's temp directory, write only the
clone and that directory, and open one TCP connection target: the forwarder.
It cannot read the endpoint key, the user's agent homes (`~/.codex`,
`~/.claude`, `~/.hermes`), credential stores, the source checkout or other
sessions, open the user's terminals, reach the model endpoint, other loopback
services (Hermes, herdr, the model proxy itself) or the internet, or signal or
inspect (argv, environment) processes outside the sandbox. Residual risks,
accepted:

- A child command can call the forwarder too, and spend model requests as
  Codex itself could. It never sees the key, the forwarder admits only the
  configured model and the two paths, and the per-run request cap bounds the
  spend; the key is a dedicated, revocable headless key.
- Seatbelt names loopback only as `localhost`, so the network rule also
  matches `::1` at the forwarder's port; the forwarder holds that address
  itself (bound, not listening), so nothing answers there.
- The forwarder token is in `config.toml`, which the agent can read; it keeps
  other local processes, not the agent, from using the forwarder.
- The sandbox can read `PATH` directories and toolchain trees; secrets kept
  there (outside the denied paths) are readable. Keep the key file in
  `~/.config/harness-launcher`; a key readable inside the sandbox is refused.
- Codex reaches the model with the full conversation each request; the
  forwarder checks the model name, not the content.

### Headless target mode (code repositories)

`--target <name>` points a headless Codex run at one personal code repository
the owner registered, instead of the harness. Everything else in the Codex
path (forwarder, `CODEX_HOME`, process-group and lingering-process kills,
result file) is unchanged; the session, delivery and verifier differ.

- **Arguments.** `--target` needs `--agent codex` (with `--agent claude` it is
  `refused`: the Claude sandbox is a deny list and cannot promise to keep
  other code and the keychain out), `--target-digest` (64 hex),
  `--task-id` (`[A-Za-z0-9][A-Za-z0-9_-]{0,63}`) and `--approval-sha` (a
  lowercase hex SHA). The companions without `--target` are `refused`. The
  launcher names the branch `loop/<task-id>-<approval-sha[:8]>`.
- **Owner policy.** The owner-specific values are host configuration, never
  launcher code: `<profile home>/target-policy.json` (normally
  `~/.config/harness-launcher/`; a regular file owned by the user, mode 0600,
  one link) holds exactly:

  ```json
  {"owner": "example-owner",
   "ssh_hosts": ["github.com-example"],
   "deny_repos": ["example-owner/example-harness", "other-org/*"],
   "home_deny": ["company-*", "mirrors", ".agent-state"]}
  ```

  `owner` is the GitHub account; `ssh_hosts` the SSH host aliases a remote may
  use (never plain `github.com`, whose default key may be another account's);
  `deny_repos` repositories that are never targets; `home_deny` paths under
  `HOME` (a trailing `*` matches a name prefix) that no live checkout may sit
  under and the agent sandbox denies; `ssh_hosts` and `home_deny` must not be
  empty, and `target add` shows all four values on its confirmation screen.
  The owner writes it by hand
  (`install -m 600`). Without it, or with any other shape, every `target`
  command and every `--target` run is `refused`.
- **Host record.** `<profile home>/targets/<name>.json` (a 0700 directory)
  holds exactly the canonical JSON of the entry `{name, github, remote, base,
  test: {group, run, timeout_min}}`: `json.dumps(entry, sort_keys=True,
  separators=(',', ':'), ensure_ascii=False)` in UTF-8, `~` never expanded;
  `path` and `about` from the registry are not part of it. The run refuses
  unless the file is a regular file owned by the user, mode 0600, one link,
  its sha256 equals `--target-digest`, and it is the canonical JSON of its own
  entry. `<name>.path` beside it (same file rules, optional) holds the realpath
  of the registry `path` (it must be absolute): the owner's live checkout,
  which a run never reads or writes and the agent sandbox denies.
  `test/fixtures/target-digest-vectors.json` holds shared vectors (YAML entry,
  canonical JSON, sha256; placeholder values) for the loop.
- **Registering.** `harness-profile target add <name> --from <targets.yaml>`
  needs a terminal on stdin and stdout, checks every boundary rule below
  (network ones included), shows the entry and its unified diff against the
  current record, and writes only after the owner types the name again
  (atomic rename, 0600). `target list` and `target show <name>` print records
  and digests. The terminal check is an accident guard, not a security
  boundary: an agent or loop cannot answer it only because nothing sandboxed
  can write the profile home (the Codex profile denies it, and the Claude
  lane's settings deny writes to the profile home in use in the sandbox and
  the edit tools). YAML is read with PyYAML when the launcher interpreter has
  it, otherwise with the system Ruby's `YAML.safe_load`.
- **Boundary.** Enforced by `target add` and again by every run, before the
  agent and again before the push; any mismatch is `refused`: the `github`
  owner is the policy `owner`; the remote is `git@<alias>:` with an alias from
  `ssh_hosts`, followed by exactly `<github>.git`; the repository matches no
  `deny_repos` entry; the live path is not under a `home_deny` entry;
  `ssh -T git@<alias>` greets the owner; `gh auth token --user <owner>` works
  (from launchd or a cron job too; otherwise `refused` with the reason);
  `gh api user` is the owner; `gh api repos/<github>` is `private: true` and
  `fork: false`; `gh api repos/<github>/actions/permissions` is `enabled:
  false` (GitHub Actions off for the repository: no workflow can run on the
  pushed branch or the PR, whatever its triggers, per-workflow permissions or
  environment secrets); the default workflow permissions are `read`; the
  repository has no Actions secrets.
- **Clone.** In `<state>/target-runs/<run id>/root`: `git init`, then `git
  fetch <remote> +refs/heads/<base>:refs/remotes/origin/<base>` with `-c
  gc.auto=0 -c core.hooksPath=/dev/null`, `GIT_CONFIG_GLOBAL=/dev/null` and
  `GIT_CONFIG_NOSYSTEM=1`; the root is detached at the fetched base SHA
  (`base_sha` in the result), and its device and inode are recorded. The live
  checkout is never used.
- **Frozen root.** The agent can write its root, which includes removing it
  and putting a symlink or another directory in its place; git would follow a
  symlink and stage sandbox-denied files. After every agent process is gone
  the broker renames the root to `root.frozen` beside it (a directory the
  agent cannot write) and requires a real directory with the recorded inode
  there; otherwise the run is `failed` and nothing is staged. Every later
  step uses the frozen path. Interactive and headless harness sessions
  (`session-isolation.sh`) record the root's inode at `create` and refuse
  (exit 10, `refused` for headless runs) to stage a root that is not that
  directory; records from older launchers fall back to a real directory whose
  physical path is the recorded one.
- **trusted.git.** A bare repository in the delivery record
  `<state>/target-delivery/<name>/<task>-<approval8>/`, outside the session
  root, made before the agent starts with the base commit and an index of the
  root. After the agent, the broker stages the frozen root with that index
  (`add -A`), writes a binary patch and a manifest, applies the patch onto a
  fresh checkout of the base with its own index, and requires the resulting
  manifest to be identical. Commit (`commit-tree`, author `harness-broker`),
  push and readback use only trusted.git, with `-c core.hooksPath=/dev/null
  -c core.fsmonitor=false` and the other hardening options,
  `GIT_CONFIG_GLOBAL=/dev/null` and `GIT_CONFIG_NOSYSTEM=1`; the agent clone's
  `.git` (config, hooks, fsmonitor) is never read, and attribute filters it
  names have no definition.
- **Rules.** `CODEX_HOME/AGENTS.md` is only `work-scale.md`,
  `outcome-control.md` and `performance-observation.md` from the harness's
  `origin/main:.claude/rules/` (`git cat-file`, never the work tree); the
  repository's own `AGENTS.md` is read by Codex as usual.
- **Test environment.** Before the agent starts, the broker checks the base
  out into `<run>/env-source` and runs `uv sync --frozen --no-build
  --no-install-project --group <test.group>` there, outside every sandbox,
  with network, and with `UV_PROJECT_ENVIRONMENT=<run>/env` (broker-owned,
  outside the run's temp base and the session root, which the agent can
  write). `--no-build` keeps sdist builds (package code) from running outside
  the sandbox; a test group that needs one is `refused` with that reason. The
  agent gets `HARNESS_TARGET_PYTHON=<run>/env/bin/python`. The venv and
  `realpath(sys.base_prefix)` of its python (probed with `python -I -S`;
  neither `/`, nor an ancestor of `HOME` or of the state home, nor at or
  under the state home) are read and
  exec only in the agent and verifier profiles; every write there is denied.
  `env-source/` and the broker's `candidate/` copy are removed when the run
  ends.
- **Agent sandbox.** The 0.47 Codex profile; then read and exec of the env;
  then every path deny of that profile again (so the `base_prefix` allow
  cannot reopen one; the state-home lines are not repeated, since they would
  close the env); then denies of `~/Library/Keychains`,
  `~/.config/harness-launcher` and the profile home in use, the policy
  `home_deny` entries, the harness root and the live checkout, each also by
  resolved path. The verifier profile gets the same additions.
- **Broker checks, in order, after every agent process is killed.** No
  changes: `no_changes`. A path with a `.github` component (NFKC, case
  folded), a gitlink (mode 160000) or any `.gitmodules` change: `refused`. A
  secret pattern (auto-deliver's list, `gh[opsur]_…`, `github_pat_…` and any
  `-----BEGIN … PRIVATE KEY-----`) in an added diff line, the commit message
  or the PR body: `refused`, naming only where and which pattern. Any change,
  at any depth, to `uv.lock`, `uv.toml`, `.python-version` or
  `requirements*.txt`, or to `[project] dependencies`, `optional-dependencies`
  or `requires-python`, `[dependency-groups]`, `[build-system]` or `[tool.uv]`
  of a `pyproject.toml` (compared as parsed TOML; a file that no longer parses
  counts): `failed`, "의존성 변경은 오프라인 검증 불가". Then the verifier:
  `test.run` with `argv[0]` (always the literal `python`) replaced by the env
  python, on a copy of the candidate, under the verifier profile plus the
  additions above, with a minimal environment and `test.timeout_min`; a
  nonzero exit or timeout is `failed` (the verifier rejection reason).
  Nothing is pushed in any of these cases.
- **Delivery.** The broker commits, writes the manifest, message, PR body,
  the record digest and `PENDING(<sha>)` to the record's journal, and runs
  the boundary preflight again. `gh auth token --user <owner>` is the token;
  it goes only into the environment of each `gh` call, never into argv, logs,
  the result, the PR body, the agent or the verifier. Then: the remote branch
  at that SHA resumes; an absent branch is pushed without force and the
  journal becomes `PUSHED(<sha>)`; a branch at another commit is `conflict`;
  a `PUSHED` journal whose branch is gone is `failed` (never pushed again).
  A fresh bare repository fetches the branch back and must find the SHA and
  the manifest.
  `gh pr list --repo <github> --head <branch> --state all` runs first; only
  when it is empty,
  `gh pr create --repo <github> --base <base> --head <branch> --draft --title <subject> --body-file <file>`,
  from an empty broker-owned directory holding only that file. After a create
  failure or timeout the list is read again before giving up. The journal
  becomes `DELIVERED(<sha>)` and `pr-url` is kept. The PR body opens with a
  note that `loop/` may not follow the repository's branching strategy, and
  the agent's message body is inside a fenced `text` block (shown, never
  rendered as Markdown or images).
- **Rerun.** A run whose record journal is `PENDING` or `PUSHED` does not
  start the agent: it resumes delivery of the recorded commit, and only under
  the record digest it started with (another digest is `failed`). A
  `DELIVERED` record returns its `pr_opened` result again. A record that never
  reached `PENDING` is moved aside (`.retired-*`) and the run starts fresh.
- **Result.** Additive to version 1: status `pr_opened`, and `target`,
  `branch`, `base_sha` and `pr_url` (null until known) on every `--target`
  result; `commit` is the pushed SHA.

Target mode threat model. The owner's keyring token, SSH key and live
checkout stay outside every sandbox: only the broker, after the agent's
processes are gone, runs `gh` and pushes, and only to a new branch of a
private, non-fork repository with GitHub Actions disabled, as a draft PR a
human reviews before anything is merged or deployed. What the agent writes
reaches the remote only as file content through the frozen root, the patch,
the manifest and the checks above. Residual risks, accepted:

- `uv sync` runs outside the sandbox with network. With `--no-build` no
  package build runs, but uv itself resolves and downloads the base branch's
  locked wheels (owner-reviewed content, before the agent ran) and reads the
  user's uv configuration; a compromised index or wheel is installed into the
  env, whose code then runs only inside the sandboxes.
- Actions are checked off at preflight; an owner who turns them on later is
  refused at the next run, not retroactively.
- The secret scan is pattern based; the PR is a draft and needs a human.
- Delivery records and run directories under `<state>/target-*` (the frozen
  root and the env) are kept; there is no automatic cleanup yet.

### Restore fidelity and the launch record

A host restore (`claude --resume <id>`, `codex resume <id>`) restores the
session's model and effort from its transcript or rollout, and its permission or
sandbox grant from a launch record. The two sources have different trust:

- A transcript or rollout is written by the agent, so the launcher treats it as
  untrusted data. It is read by a bounded helper (no symlink or non-regular file,
  last 8 MiB, time limit), and only model and effort are taken, each re-validated
  against a fixed vocabulary before use. A forged `permissionMode`,
  `approval_policy` or `sandbox_policy` in it is never read.
- The launch record is written by the launcher's own `SessionStart` hook from the
  grant the launcher exported for that launch, under
  `<state>/launch-records/<agent>-<session_id>` with mode 0600.
  The launcher never escalates: without a record it keeps the default grant and
  prints the command that relaunches with bypass. A record is honoured only for its own harness root,
  and it is also the only proof that a Claude session lives outside an isolated
  root. The record protects against content in agent-written session files; it
  does not stop a process running as the same user from writing the file, exactly
  like every other file in the state directory.

Three details keep the record honest. A Claude hook takes its grant as command
arguments inside the launcher's `--settings`, never from the environment, because
Claude applies the agent-writable `env` block of `.claude/settings.local.json`.
Codex's `hooks.json` row is static, so it reads the launcher's environment, and
`$CODEX_HOME` (`.harness/codex`) sits inside the workspace-write root, which makes it a
pre-existing lever inside the same boundary. Whether a launch is nested is decided by
the hook from process ancestry (a second `claude` or `codex` executable above the agent
that ran the hook), not from environment variables, so a stale agent environment in a
pane shell does not demote a restore and `env -u CLAUDECODE` does not promote a nested
launch. A nested launch may keep or lower a record's grant and never raise it, and it
can create a grant-less `isolated=0` record, which only identifies a canonical-owned
session (an isolation-default profile then refuses the resume or, for a pure Codex resume, starts a fresh
isolated session): an isolated owner still wins. The nesting rule stops accidental nesting, not a deliberate
agent: a same-user process can still write the record file directly, double-fork so it
is reparented to launchd, or type the launch into a pane shell (herdr, tmux, osascript),
and each of these counts as top-level. That stays the documented same-user boundary.

Known limits, kept on purpose:

- A raw `codex exec` or `codex exec resume` inside a Codex agent inherits the parent's
  `HARNESS_LAUNCH_*` environment, but its own hook sees the parent `codex` as an ancestor
  and is treated as nested. Ancestry recognises only executables named exactly `claude`
  or `codex`; an agent started through a wrapper with another name, such as an npm
  install whose process is named `node`, is not seen, and the walk then treats the next
  recognised ancestor as the owner.
- The hook reads the process table with `ps`. Where that fails, for example if a Codex
  hook runs inside a sandbox that denies it, the launch counts as nested and records no
  grant. The opt-in Codex row has not been checked against a real Codex session.
- A caller `--settings` after `--passthrough` replaces the launcher's, so that launch has no record.
- A planted canonical rollout in the source `CODEX_HOME` can steer a pure Codex resume to a fresh isolated session through durable history, or make another form refuse.
- The Codex hook row names the Python interpreter by its Cellar path, which changes after `brew upgrade` until the next prepare.

See [Terminal runtimes](terminal-runtimes.md#restore-fidelity) for the argv rules.

## Browser and plugin trust

The launcher can materialize supported Codex bundled plugins when a compatible marketplace source or existing cache is available.

Security rules:

- Terminal Codex does not enable the desktop-only Browser plugin surface.
- Chrome bridge support accepts current and known legacy native-host names.
- `node_repl` trusts exact browser-client SHA-256 values.
- Project-writable `CODEX_HOME` and global `~/.codex` directories are not added as broad trusted code paths.
- Codex folder trust: generated `config.toml` keeps the user's `[projects."<path>"]` decisions and adds `trust_level = "trusted"` only for the launcher's own roots (the harness root, plus the source and session roots of an isolated session), keyed by physical path. Nested repositories stay untrusted until the user decides.
- Marketplace synchronization completes before generated config reads plugin versions or browser hashes.
- Complete marketplace content, not one manifest, determines cache freshness.

## Auth boundary

Generated Codex homes link to the active native Codex auth file. The launcher does not copy, serialize, or convert OAuth refresh tokens.

A native account switch therefore happens in the Codex auth store, not by editing each project's generated home. Contributors must not add logs or diagnostics that print auth JSON or environment values.

## Gateway boundary

Gateway modes source local files under `config/.local/` and probe the configured `/health` endpoint before launching Claude Code.

Anything sent through a gateway leaves the local runtime boundary. Users must trust the gateway endpoint and its operator. Gateway configuration and credentials should never be committed.

## Failure behavior

The launcher prefers visible failures over silent fallback when a boundary is ambiguous:

- missing project config stops registration;
- duplicate MCP server names stop launch;
- missing explicit runtime binary stops launch;
- lock acquisition timeout stops shared cache mutation;
- isolated-session apply, verification, remote comparison, or verified readback mismatch remains a recoverable `CONFLICT`; a post-push readback outage remains `INTEGRATING` for remote-history reconciliation;
- failed runtime-home preparation stops the selected runtime;
- app-bundled Codex fallback requires explicit opt-in.
