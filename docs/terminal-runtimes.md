# Terminal runtimes

The launcher runs inside several terminal hosts: herdr, Orca, cmux, or a plain
terminal. Each host exports its own variables and expects its own behavior from
an agent. Since v0.36.0 the launcher detects exactly one host per launch, removes
every other host's variables, and enables host-specific features only for the
detected host. This guide is the runtime-generic reference; Orca-only material
stays in [Orca ADE integration](orca-integration.md).

## Detection

Detection order is `herdr > orca > cmux > plain`. The first match wins and
exactly one value is used:

| Runtime | Markers |
| --- | --- |
| herdr | `HERDR_ENV=1`, `HERDR_PANE_ID` non-empty, and `HERDR_SOCKET_PATH` names a socket |
| orca | `ORCA_TERMINAL_HANDLE` non-empty, or `TERM_PROGRAM=Orca` |
| cmux | `CMUX_WORKSPACE_ID`, `CMUX_TAB_ID` and `CMUX_SURFACE_ID` all non-empty |
| plain | none of the above |

herdr ranks first because a herdr pane inherits the variables of whatever
started its server (Orca or cmux), yet the pane belongs to herdr. Its socket
test rejects `HERDR_*` variables left exported after the pane is gone. Orca
outranks cmux because cmux variables linger inside Orca terminals.

A launch without a terminal, meaning stdin or stdout is not a TTY, is runtime
`plain` regardless of markers. SDK hosts such as Paseo inherit their daemon's
markers but host no terminal.

## Scrub and announcement

Every launch scrubs the environment before doing anything else, in this order:

1. Unset `CODEX_HOME` and `ORCA_CODEX_HOME` when they are equal (Orca's managed
   home, see [CODEX_HOME sanitization](orca-integration.md#codex_home-sanitization)).
2. Unset `CMUX_*` unless the runtime is cmux.
3. Unset `ORCA_*`, and `TERM_PROGRAM` when it is `Orca`, unless the runtime is
   orca.
4. Unset `HERDR_*` unless the runtime is herdr.
5. Export `HARNESS_TERMINAL_RUNTIME` with the chosen value.

The scrub runs in the agent's process, never in your interactive shell. The
cmux title brokers start only when the runtime is `cmux`.

When the runtime is herdr, the launcher writes the run directory to the
controlling terminal as an `OSC 7` sequence: ESC `]7;file://` followed by the
percent-encoded absolute directory (empty host, so `file:///...`) and BEL. herdr
uses it to type a pane restore in the right directory. herdr keeps the last
reported directory, so when the agent (or the interactive launcher, even after a
quit without a launch) returns, the launcher sends a second `OSC 7` naming the
caller's working directory: for `harness-exec` and the entry points built on it,
the directory it was started in; for the in-shell `codex` wrapper, the shell's
`$PWD`. A process that replaces the launcher with `exec` never returns to it, so
nothing re-announces the caller's directory after such an agent exits. No other
runtime receives the sequence.

## Ownership

Who supplies each capability, per runtime:

| Capability | herdr | Orca | cmux |
| --- | --- | --- | --- |
| Title | Runtime-native: OSC title from the agent (Claude `sessionTitle`, Codex `[tui] terminal_title`) | Runtime-native: OSC title | Launcher: the existing title brokers, cmux runtime only |
| Claude state and notify | Runtime-native screen detection and toasts; `herdr integration install claude` reports session identity | Runtime-native: Orca hooks | Runtime-native: cmux `claude` wrapper |
| Codex state and notify | Runtime-native screen detection; opt-in (`HARNESS_HERDR_AGENT_HOOKS=1`) launcher registry row for the identity hook | Launcher registry row calling the Orca hook (`HARNESS_ORCA_AGENT_HOOKS=1`) | Harness: the existing harness cmux adapters (`codex-subagent-status.sh`) |
| Kiro | Not detected (known limit) | Runtime-native: Orca | Unchanged |

Harness-side adapters exist only for a runtime that fails acceptance for a
capability; the launcher and the harness own the policy hooks, auth, MCP, skills
and model presets in every runtime, never the host; the launcher owns detection, scrub, `OSC 7`, the
Codex hook rows, resume mapping and the recorder command
(`harness-session-provider-record`).

Exclusivity: each native hook exits without its own runtime's variables (herdr
needs `HERDR_SOCKET_PATH`, Orca needs `ORCA_AGENT_HOOK_PORT`, cmux needs
`CMUX_SURFACE_ID`). The scrub leaves only the current runtime's variables, so
only that runtime's native hooks act, even though all three may be registered
in the user-global `~/.claude/settings.json`.

## Codex hook registry

Host status hooks for Codex are rows of one registry in `codex-home-prepare.sh`,
each enabled per harness by a literal assignment in that harness's
`config/launcher.env`. The process environment is ignored, so every caller
generates the same `hooks.json`.

| Runtime | Opt-in | Script | Events | Argument | Timeout (seconds) |
| --- | --- | --- | --- | --- | --- |
| orca | `HARNESS_ORCA_AGENT_HOOKS=1` | `$HOME/.orca/agent-hooks/codex-hook.sh` | `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PermissionRequest`, `PostToolUse`, `Stop` | none | 5 |
| herdr | `HARNESS_HERDR_AGENT_HOOKS=1` | `$HOME/.codex/herdr-agent-state.sh` | `SessionStart` | `session` | 10 |
| launch_record | `HARNESS_LAUNCH_RECORD_HOOKS=1` | `harness-launch-record codex` (launcher-owned) | `SessionStart` | none (`--callback` and timeout 10 when the ssot opt-in is also on) | 5 |

The `orca` and `herdr` rows run the same status-only, fail-open command:

```text
/bin/sh -c 's="<script>"; [ -x "$s" ] && { /bin/sh "$s" [argument] >/dev/null 2>&1; exit 0; }; cat >/dev/null'
```

The script's output is discarded and the command always exits 0, so a host
script can never block a tool or decide a permission. When the script is not
executable the command only drains stdin. An opted-in row is written whether or
not the script exists, so installing a host integration later needs no new
prepare. Rows are appended after every existing entry in registry order.
Without an opt-in, `hooks.json` is identical to earlier releases. After
enabling or disabling an opt-in, rerun the harness's Codex hook trust step.
Isolated sessions use fresh Codex home clones without that trust, so host status
for isolated Codex sessions is not provided.

The `launch_record` row is not a host script. It runs the launcher's own
`harness-launch-record codex` (Python, no output, exit 0) and is described under
[Restore fidelity](#restore-fidelity). Enabling it changes `hooks.json`, so rerun
the harness's Codex hook trust step afterwards; Codex will not run an untrusted
hook.
With the ssot opt-in also on, the row becomes `harness-launch-record codex --callback`
(timeout 10), and carrying it into an isolated home additionally requires a
`callbacks` entry with the sha256 of
`$HOME/.local/share/harness-service/bin/harness-session-hook` in the hook-approval
grant. Only `SessionStart` runs the host callback in such carried sessions
(see [session-collector](session-collector.md)).

## Resume routing

A host restores an agent by re-running its command with `--resume <id>` (Claude)
or `codex resume <id>` (Codex). For profiles that default to isolated sessions,
the launcher maps that single UUID to the isolated session that owns it. Owners
are counted together:

- For Claude, the session directory whose name equals the id (the mapping from
  v0.33.0).
- For Claude, every current-harness session whose `provider-sessions` file has a
  line `claude <id>`. This owner maps only when the transcript `<id>.jsonl`
  exists under the Claude project directory of that session's root or of one of
  its recorded run directories (below).
- For Codex, the one session whose Codex home holds the matching rollout file.

A session both named by the id and recording it counts as one owner. The
directory owner maps without the transcript check. Exactly one owner maps; two
or more owners is rejected as ambiguous; no owner keeps the usual reject
message. The session must belong to the current harness.

`harness-session-provider-record` is the recorder. It reads a Claude
`SessionStart` hook payload on stdin, handles the sources
`startup|resume|clear|compact|fork`, and appends `claude <uuid>` to
`<state>/sessions/<HARNESS_SESSION_ID>/provider-sessions` for UUID ids only. It
is silent and exits 0 in every other case. The launcher only ships the command;
each harness registers the recorder through its own `SessionStart` hook shim,
which is a separate change in that harness.
Until a harness registers it, `/clear` ids stay unmapped. The recorder cannot
tell a Codex `SessionStart` payload from a Claude one, so each harness excludes
its shim from Codex through `codex_exclusions`; the transcript check is the
backstop.

Claude files a transcript under the project directory of its working
directory. Through `harness-exec` (and so `harness-auto`, shell routing, Orca and
herdr restores) an isolated agent runs in the caller's directory when that is
inside the harness, else in the source harness root: `harness-exec` always
passes `--cwd`. Only a launch without `--cwd` runs in the session root. Before
the agent starts, the launcher appends that resolved run directory to
`<state>/sessions/<HARNESS_SESSION_ID>/run-dirs`, one path per line. It skips an
identical line, never writes through a symlink, writes only a regular file, and
records only a directory inside the session's source root or session root.
The resolver reads `run-dirs` only as a regular non-symlink file and uses a line
only when it is canonical (an absolute path that equals its own resolved form,
as the launcher writes it) and lies inside that session's source root or session
root. It checks the session root and each such line. The owner count is
unchanged, so a forged line can only confirm a transcript for an id its session
already owns by record; an id another session also owns is ambiguous and
rejected, and an id no session owns stays rejected.

Limits:

- Restore fails closed before v0.36.0: earlier launchers cannot map ids created
  after `/clear`.
- The transcript check cannot tell a nested `claude -p` (same project
  directory, inherited `HARNESS_SESSION_ID`) from the session itself: the
  nested run's id is recorded and its transcript exists. Such an id maps to the
  same session. The owner count still prevents mapping into another session.
- Sessions of the same harness that ran in the same directory share one Claude
  project directory, so the transcript check alone does not tell them apart;
  the `provider-sessions` owner count does.
- A manifest-enabled isolated Codex session inherits unchanged approved herdr
  and Orca status hooks after broker/source verification. Unapproved or changed
  callbacks retain native review.
- A plain `<prefix>` launch that opens the interactive picker has no session id
  to map.

### Explicit Claude fork verification

Release-owned entry integrations can use `bin/harness_claude_fork.py` to handle
`--resume <native-uuid> --fork-session` before allocating a new owner. An exact
resume alone never authorizes a fork. `request()` recognizes direct Claude
resume controls, option values and the `--` prompt boundary; `rewrite()` replaces
only the selected UUID with the verified absolute transcript path and supplies
`--permission-mode default` unless the caller explicitly selected permissions.
It keeps launcher presets before `--passthrough` and native arguments after it,
so a bare fork starts Claude directly and native option values cannot become
launcher keywords.
The launch planner likewise requires a fresh caller permission for a Claude
fork; the parent's saved permission is not inherited.

`prepare(source_root, state_home, native_id, home=None)` acquires the native-ID
lease before catalog discovery, then acquires the known parent's `runtime.lock`.
Only a unique source-owned `CLOSED`, `DELIVERED` or `DISCARDED` parent with an
inactive lease is eligible. It verifies the provider binding, journal, source,
recorded physical working directory and bounded transcript hash. Recorded clone
subdirectories remain valid after retirement; sibling prefixes and symlink
escapes do not establish ownership. Symlinked paths,
unsafe files, ambiguous ownership and unknown activity fail closed. A retired
parent's disposable root may be absent if its durable ownership proof and global
Claude transcript remain. Canonical history without provable inactivity remains
on hold.

The returned object has `receipt`, inheritable `fds`, `recheck()` and idempotent
`close()`. The entry must allocate a fresh owner from its current release, add
that owner's UUID as `owner` to the receipt, and write it privately and immutably
as `<state>/sessions/<owner>/claude-fork-origin.json`. The receipt records schema,
runtime, source root, parent native ID and owner, parent status, native home,
absolute locator, SHA-256 and byte count. It copies no auth, config or grants.
The recorder refuses the parent's native UUID and refuses malformed or rebound
receipts; only a distinct child UUID can become the new owner's provider ID.

Keep both leases for the whole derived run and call `recheck()` immediately
before native execution. POSIX record locks survive `exec`, but a forked child
does not own them: a supervising entry must remain alive until that child exits.
Do not rediscover the catalog or reopen either lock file in the holding process;
closing another descriptor for the same inode would release its record lock.
Verification failures close acquired leases. Launcher leases coordinate managed
writers; unrelated raw CLI writers do not participate in that guarantee.

## Restore fidelity

A host restore carries no model, effort or permission, so the launcher would
apply its `base` defaults and lose what the session had. For a **pure resume**
the launcher instead restores the session's own settings.

Detection (Claude and Codex):

- Shell routing marks the `base` it adds to a typed `claude` with
  `HARNESS_HOST_DEFAULT_MODE=base`, only for that `harness-auto` call. The
  launcher reads the marker at the start of `_harness_launcher_run` and unsets it
  before any agent starts, so the marker never reaches an agent or a nested
  launch. A user-typed `<prefix> base --passthrough --resume <id>` has no marker
  and keeps `base`.
- A pure resume is exactly one resume of exactly one UUID and nothing else:
  Claude `--resume <id>`, `--resume=<id>` or `-r <id>`; Codex `resume <id>`. Any
  caller `--model`, `--effort`, `--permission-mode` (Claude) or `-m`, `-p`,
  `--profile`, `-a`, `-s`, `--full-auto`,
  `--dangerously-bypass-approvals-and-sandbox` (Codex), any other argument, or an
  explicit launcher keyword (`rich`, `sol`, `bypass`, ...) makes it not a
  restore, and the caller's choice stands. Orca's `<override> <default args>
  --resume <id>` with keyword defaults is therefore left alone; without them
  (`<prefix> --resume <id>`) it restores.
- A detected Claude restore always launches directly, so `<prefix> --resume
  <id>` no longer drops the id into the interactive picker.

Model and effort come from the session file, through `harness-restore-probe`
(run with the resolved Python 3.11+):

- Claude: the last main-thread (non-sidechain) assistant `message.model` that is
  a Claude model (`<synthetic>` and gateway names are skipped), and the `effort`
  of the same entries. `[1m]` is added when any of three signals shows a 1M context. The
  first is the latest `cost-state` `modelUsage` having `<model>[1m]` in the
  window; that map accumulates over the session, so it is imprecise, and a long
  transcript may keep none in the 8 MiB tail. The second is usage: the largest
  `input_tokens + cache_read_input_tokens + cache_creation_input_tokens` of a
  main-thread turn in the window (each count an integer of 0 or more, sidechains
  ignored) above 200000, which a 200k context cannot reach. The third is the launch
  record's `context=1m`, below.
- Codex: the last `turn_context` `model` and `effort`, and `context=1m` when the
  rollout recorded a context window of at least 500000 tokens (otherwise
  `272k`). An explicit `272k`/`1m` keyword wins.
- The values become `--model`/`--effort` in the passthrough argv before caller-wins
  reconciliation, so a restored `xhigh` or `max` turns thinking on the same way a
  caller effort does. For Codex they are `-m <model> -c
  model_reasoning_effort="<effort>"` after the profile flags.
- The helper opens the file with `O_NOFOLLOW|O_NONBLOCK`, requires a regular file
  (no symlink, FIFO or directory), reads only the last 8 MiB and drops the first
  partial line, stops after 5 seconds, prints `key=value` lines and always exits
  0. The launcher matches each line as data (never `eval` or word splitting) and
  re-validates: model `^[A-Za-z0-9][A-Za-z0-9._-]*(\[1m\])?$` (Claude models also
  start with `claude-`), effort `low|medium|high|xhigh|max` (`minimal` for Codex).
  No file or an invalid value keeps the launcher default.

Permission and sandbox never come from a transcript or rollout, which the agent
can write. They come only from the **launch record**:

- The launcher records what it launched the agent with: for Claude the
  `--permission-mode`; for Codex `-a`, `-s`, `--full-auto` or the bypass flag, and
  the launcher-owned profile (`fast|base|sol|astra|plan|rich`, not a caller `-p`),
  plus the source root and isolation.
- A `SessionStart` hook the launcher injects runs `harness-launch-record`, which
  writes `<state>/launch-records/<agent>-<session_id>` (mode 0600). It opens the
  `launch-records` directory with `O_DIRECTORY|O_NOFOLLOW`, writes a private temp
  file and renames it through that descriptor, so a symlink at the path is
  replaced, never followed and a symlinked directory is refused. The reader
  opens the same way and also requires a regular file owned by the current user
  with a single link. Keys: `permission`, `approval`, `sandbox`, `bypass`,
  `profile=<name>`, `source_root`, `isolated=0|1`, `harness_session_id`,
  `context=1m`, and for automatic session linking (see
  [session-collector](session-collector.md)) `source`, `agent_pid`,
  `agent_start`, `task_id`, `parent_native`; each value
  is checked against a fixed vocabulary before it is written or read.
- Claude: the hook rides the launcher's own `--settings`, merged with the forced
  thinking setting into one JSON (`{"alwaysThinkingEnabled":true,"hooks":{...}}`).
  The grant, source root and isolation are arguments of the hook command
  (`--permission`, `--source-root`, `--isolated`, `--harness-session-id`), and the hook
  ignores `HARNESS_LAUNCH_*` for Claude: Claude applies the `env` block of
  `.claude/settings.local.json`, which an agent can write. A caller `--settings`
  after `--passthrough` is passed last and may replace it; the launch then simply
  has no record.
- Context. When the final `--model` (profile or passthrough, the last wins) ends in
  `[1m]`, the Claude hook command also carries `--context 1m`, and the record stores
  `context=1m`. A Claude restore appends `[1m]` to a probed model that has no suffix
  when the record says so (never inventing a model, and the result must pass the
  model check). `context` is not a grant: it only sizes the context window, so the
  nested rank rules do not apply and nothing about permissions depends on it.
- Codex: the `launch_record` registry row above (opt in with
  `HARNESS_LAUNCH_RECORD_HOOKS=1`). Its `hooks.json` row is static, so the hook
  reads the launcher's environment (`HARNESS_LAUNCH_APPROVAL`, `_SANDBOX`,
  `_BYPASS`, `HARNESS_LAUNCH_PROFILE`, `_SOURCE_ROOT`, `_ISOLATED`),
  exported around the agent process only, inherited values cleared first.
  Manifest-enabled isolated Codex homes inherit a verified unchanged canonical
  approval for this callback; missing or changed approvals retain native review.
- Nested launches. The hook decides from process ancestry, not from the
  environment: starting at the `claude` or `codex` process that ran the hook, if
  any further ancestor's executable basename is exactly `claude` or `codex`
  (at most 64 hops, stopping at pid 1), the launch is nested; an unreadable
  process table also counts as nested. A nested launch keeps or lowers an existing
  record's grant, never raises it, keeps the record's source root and isolation,
  and creates a record without a grant when none exists. Grants rank
  `plan` < `dontAsk` < `default` < `acceptEdits` < `auto` < `bypassPermissions`;
  Codex approval `untrusted` < `on-failure` < `on-request` < `never` and sandbox
  `read-only` < `workspace-write` < `danger-full-access`. A top-level launch (your
  terminal, or a herdr-typed restore from a pane shell whose ancestors are only
  herdr, shells and init) sets or raises, so the relaunch command the hint prints
  does record its grant. A stale agent environment in a pane shell does not make it
  nested, and `env -u CLAUDECODE` does not make a nested launch top-level.
- On restore the launcher reapplies the recorded grant, only if the record names
  this harness root. For Codex it first reapplies the recorded `-p <profile>`,
  because a profile can carry a grant of its own (`plan` is read-only), and only
  while `$CODEX_HOME/<profile>.config.toml` still exists as a regular file;
  otherwise it keeps `base`. Model and effort from the rollout follow the profile.
  The launcher keeps the default and prints one line with the exact command that
  relaunches with bypass when there is no record at all (every session started
  before this feature), or the recorded Codex profile is gone. A record without a
  grant means the user chose the default mode, so it stays quiet. For example `<prefix> rich bypass --passthrough --resume <id>` or
  `<prefix> codex sol bypass --passthrough resume <id>` (the keyword follows the
  restored model). Claude modes carry no permission, so there is no Claude profile
  to restore. It never escalates from a session file.
- Sessions started from the interactive picker (`launcher.sh`, v0.39.1+) write the
  same record: it passes the same Claude hook arguments and exports the same Codex
  `HARNESS_LAUNCH_*` from its menu choices (`harness-common.sh` holds both for the
  two paths). A picker launch through `happy` still writes none for Claude; Codex
  through `happy` records no profile and no grant, as on the shortcut path.

Canonical owner. `harness-launcher` maps a restore id to the isolated session that
owns it (above). When no isolated session owns the id, `_harness_launcher_resolve_restore`
returns 4 in two cases, and an isolation-default profile then takes the legacy
(non-isolated) route instead of rejecting:

- Codex: the rollout `<harness>/.harness/codex/sessions/*/*/*/rollout-*-<id>.jsonl`
  exists (regular, not a symlink) in the source harness `CODEX_HOME`.
- Claude: a launch record says `isolated=0` for that id and this harness root.
  Isolated sessions file their transcripts in the same project directory, so a
  transcript alone is never proof.

An isolated owner always wins. `HARNESS_SESSION_ISOLATION=1` (forced) and every
other case keep the reject message. A Claude session started before launch
records exists still rejects in an isolation-default profile; relaunch it once
with `--no-isolated` to start recording.

## Host prerequisites

These are one-time setup steps on the machine, verified on macOS.

herdr:

- Put this line in `~/.zshenv`. It skips the Kiro CLI PTY wrapper, which hides
  agents from herdr, and only inside herdr panes:

  ```zsh
  if [[ -n "$HERDR_ENV" ]]; then export PROCESS_LAUNCHED_BY_Q=1; fi
  ```

- Report ordinary `cd` to herdr. Oh My Zsh's own `OSC 7` carries a hostname,
  which herdr drops, so add an empty-host report to `~/.zshrc` for herdr panes:

  ```zsh
  if [[ -n $HERDR_ENV ]]; then
    _herdr_osc7() {
      local LC_ALL=C c enc=
      for c in ${(s::)PWD}; do
        [[ $c == [A-Za-z0-9/._~-] ]] && enc+=$c || enc+=$(printf '%%%02X' "'$c")
      done
      printf '\e]7;file://%s\a' "$enc"
    }
    autoload -Uz add-zsh-hook && add-zsh-hook precmd _herdr_osc7
  fi
  ```

- Enable shell routing (`harness_shell_enable`) in the shell herdr panes start.
  herdr restores a pane by typing a plain `claude --resume <id>` or
  `codex resume <id>`; without routing those commands bypass the launcher, so
  they run without the profile environment and outside the isolated session.
- Install the Claude integration with `herdr integration install claude`.
- For Codex, install with a pinned `CODEX_HOME` so herdr writes to the user's
  own home, then opt the harness in:

  ```bash
  env -u CODEX_HOME CODEX_HOME="$HOME/.codex" herdr integration install codex
  ```

  and add `HARNESS_HERDR_AGENT_HOOKS=1` to the harness `config/launcher.env`.
- Optional: link the [herdr plugin](#herdr-plugin) for tab labels and
  notifications that focus the agent pane.

Oh My Zsh:

- Set `zstyle ':omz:update' mode reminder` in `~/.zshrc`. An update prompt at
  shell start would swallow the resume command a host types into a restored
  pane.

Orca setup is in [Orca ADE integration](orca-integration.md).

## herdr plugin

The package ships a herdr plugin in `share/harness-launcher/herdr-plugin`
(macOS, herdr 0.9.1 or later). herdr runs it with `/usr/bin/python3` at
startup and on pane and tab events. It keeps session titles, tab labels and
sidebar values current, and posts notifications.

Session titles. A rename reaches the agent's own records before its terminal
title, so the plugin reads those first. For Codex that is the thread's latest
name in `<CODEX_HOME>/session_index.jsonl`, because a running Codex never
updates its terminal title when its thread is renamed from another app-server
connection (such as a harness title hook): the title keeps the thread id, or
the name the session resumed with. The plugin looks for
`.harness/codex/session_index.jsonl` from the pane's directory upward, then
`~/.codex`, and takes the first index that knows the thread (at most its last
16 MiB). For Claude it is the latest custom title in the session transcript,
found as `projects/*/<session>.jsonl` under `.harness/claude` from the pane's
directory upward, then `~/.claude`: a title set from outside the running
Claude (a harness title hook) reaches its terminal title only at the next
prompt. Without a custom title the terminal title wins, then Claude's AI title.
The plugin reads only regular files you own, never through a symlink. It reads a
transcript in full the first time, then only what was appended (the offset is
kept in its state). A trailing `| <name>harness` that Codex adds to its
title and the `[ . ] Action Required |` marker it blinks while waiting for
approval are dropped; a thread id is no title.

Pane metadata. Each agent pane reports its full title as herdr's metadata
title (the sidebar `pane` token), the state labels `대기`, `작업 중`, `입력 필요`
and `응답 종료` (the `state_text` token), and a `$model` token with the model and
reasoning effort of the latest turn (from the Codex rollout's last
`turn_context` or the last Claude assistant record), for example
`6.1-sol xhigh` or `opus-5-5 xhigh`. The model is read again when the agent's
status changes (from at most the last 32 MiB of the file). When the agent leaves
the pane, the plugin clears them. herdr keeps no pane metadata across a server
restart, so the plugin reports every pane again at startup, and a pane again
when an agent is detected in it.

Decision. herdr's own status says `idle` (or `done`, before you have seen the
pane) both when a turn is done and when it ended by asking you to choose. When a Claude turn ends and its last answer marks
a recommended option with `← 추천` (outside a code block), the plugin sets a
`$decision` token to `결정 필요`; the next status other than `idle` or `done`
(you answered, or the agent asks for input) or a turn that ends without the mark
clears it. herdr's state labels accept only its own statuses, so this is a token,
not a label.

Tab labels. A tab that holds a detected agent takes that agent's session title
as its label, cut to 20 display cells (a wide character counts as two) with a
trailing `…`. When a tab holds several agents, the first one with a usable
title names the tab and the others are counted after it, such as
`release notes +2`; the count stays inside the 20 cells, and plain shells in
the tab are not counted. The plugin renames a tab only while its label is
herdr's default (the tab's position in its workspace, such as `4`) or the
label the plugin set last; a name you give a tab stays. Rename a tab back to
its position number to hand it back to the plugin. When a tab it labeled no
longer holds an agent with a usable title (the agent exited, or Codex titled
the session by its thread id), the plugin renames it back to its position.
The `tab.renamed` event of the plugin's own rename does nothing.

Watcher. herdr 0.9.1 runs plugins on no event for a title or thread-name
change, so each plugin run makes sure one background watcher runs for its
herdr session (`harness_herdr_plugin.py watch`, detached; herdr gives a plugin
one state directory for all sessions, so the watcher and the per-pane records,
read offsets and found paths are kept per session API socket, while tab
ownership stays shared). It subscribes to `pane.updated` over that socket,
which reports terminal title changes of agent panes (a shell pane's title and
Codex's blinking marker do not count), and checks the session indexes and
transcripts behind the current titles once a second for appended title
records; either syncs at once, so a rename shows within about a second. It
also syncs every 15 seconds, and after herdr closes the event stream (a server
restart) it reports every pane again. It exits when the plugin file changes, as on an upgrade (the next plugin run starts the new one),
when herdr no longer lists the plugin as enabled (`herdr plugin disable` or
`unlink`), or when herdr has not answered three times in a row. Its errors go
to `watch-<id>.log` in herdr's plugin state directory. `HARNESS_HERDR_WATCH=0`
in the herdr server environment keeps a new watcher from starting; disable the
plugin to stop a running one.

A sidebar that uses these values:

```toml
# ~/.config/herdr/config.toml
[ui]
window_title = "{workspace} · {tab}"

[ui.sidebar.agents]
rows = [
  [
    "state_icon",
    { token = "pane", bold = true },
    { token = "state_text", fg = "#f38ba8", bold = true, rules = [{ equals = "입력 필요" }, { contains = "", hide = true }] },
    { token = "$decision", fg = "#f9e2af", bold = true },
  ],
  [{ token = "workspace", dim = true }, { token = "agent", dim = true }, { token = "$model", dim = true }],
]
```

The first row's `state_text` shows only `입력 필요`, in red, and `$decision`
shows `결정 필요`, in yellow. Plan usage belongs
to the account, not a pane: put [`harness-herdr-web usage`](herdr-web-ui.md) in
the tab bar and its `--watch` board in a popup.

Notifications. When an agent goes from `working` to `idle` or `done` (herdr's
status for a finished turn in a pane the user has not seen yet), or to `blocked`,
the plugin waits one second and, if the state still holds, posts a desktop
notification. The title is `✅ <agent> 응답 종료`, `🔘 <agent> 결정 필요` (a turn
that ended on a choice) or `⏳ <agent> 입력 필요`: an
ended response does not assert that the user's entire task is verified. A fresh
`herdr_activity=waiting|stalled` token paired with `herdr_activity_id` defers the
response notification until the same cycle explicitly reports `settled`.
A decision notification requests input and is delivered while work waits; it
does not release the background activity or claim completion.
Missing or expired activity after an observed wait stays uncertain; it does not
release the notification. A new foreground turn, session identity or startup
cancels the old deferred response. The subtitle is the workspace label. The
body prefers a manual pane label, then its metadata session title and terminal
title (uncut). Clicking it
activates the terminal app that hosts the herdr client (found from the client's
process ancestry, so cmux, Ghostty or another app) and runs
`herdr agent focus <pane>`. A newer notification for the same pane replaces
the older one. The visible tab stays silent while its host app is frontmost,
as herdr's own toasts do. If the plugin cannot find the host app, the visible
tab always stays silent. Notifications need `terminal-notifier`
(`brew install terminal-notifier`). herdr runs plugins with only the system
`PATH`, so the plugin also looks in the Homebrew prefix that holds herdr.
Without it the plugin falls back to `osascript`: the notification shows as
Script Editor, and clicking it opens Script Editor instead of the pane. Set
`HARNESS_HERDR_NOTIFY_DELAY_SECONDS` in the herdr server environment to change
the one-second delay.

Install once with the plugin's `install` command, and turn off herdr's own
popups so each event notifies once. Do not `herdr plugin link` the packaged
directory: herdr records a linked manifest by its resolved path, which is the
versioned Homebrew Cellar directory that the next upgrade removes. `install`
writes the manifest to `~/.local/share/harness-launcher/herdr-plugin` (change it
with `--dir`), points its commands at the script through the unversioned `opt`
path, and links that directory, so upgrades keep working:

```bash
/usr/bin/python3 "$(brew --prefix)/opt/harness-launcher/share/harness-launcher/herdr-plugin/harness_herdr_plugin.py" install
```

Run `install` again after an upgrade whose changelog mentions new plugin
events.

For a browser client for herdr panes, see [herdr web ui](herdr-web-ui.md):
`harness-herdr-web install` sets it up local-only behind a token.

```toml
# ~/.config/herdr/config.toml
[ui.toast]
delivery = "off"
```

Then run `herdr server reload-config`. The plugin keeps its state in herdr's
plugin state directory, and `herdr plugin log list --plugin harness.launcher`
shows its runs. Remove it with `herdr plugin unlink harness.launcher`.
