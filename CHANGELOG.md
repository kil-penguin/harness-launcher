# Changelog

Notable changes are recorded here. This project follows semantic versioning for published launcher packages.

## Unreleased — Usage collection

- Add a usage-collection policy: only harnesses that opt in with
  `HARNESS_AGENT_USAGE_COLLECT="1"` on an enrolled device stay visible to an
  organization-managed collector. Other profiles disable Claude telemetry through
  the launcher `--settings`, discard child-process OTLP signals, and publish
  exclusion manifests in `~/.agent-usage/collection.d`; collecting profiles
  inherit the user's Codex `[otel]` table and register isolated session roots.
  The flag is read from the canonical source root (never an isolated clone);
  reconcile evaluates every recorded session source root, leaves unevaluated
  sessions untouched, sweeps only its own state home's manifests and runs under
  a bounded cross-process lock. Codex launches with local observability point
  Claude child processes at the loopback collector, and `checkup prompt-audit`
  carries the policy in its own `--settings`.

## 0.67.0 — 2026-10-20

- Make isolated-session root identity tolerant of APFS device renumbering after
  a reboot. A recorded `root-inode` still matches when the inode is equal and the
  root sits on the device of its `worktrees` directory; a recreated directory, a
  different inode, a symlink, or a root on another filesystem is still refused.
  Applies to session submit/deliver, the Codex history catalog and pool
  protection, hook trust, native plugin inheritance and the session report.
- Make exact-id Codex history lookups cheap: `codex-history.py catalog` accepts
  `--native-id`, filtering before owner dedupe with an unchanged output schema;
  each home's inventory is hashed once per call unless its stat signature
  changes; the launcher's history selection passes `--native-id` for a UUID.

## 0.66.0 — 2026-10-19

- Default new native Codex launches to Auto, following Codex configuration and
  model metadata without launcher-pinned context or compaction values.
- Add explicit 500K context with automatic compaction at 400000 tokens alongside
  the existing 272K and 1M policies, consistently across shortcuts, the TUI, and
  Web launch plans.
- Preserve saved context choices and legacy restore behavior. Record the selected
  policy instead of inferring it from an observed runtime window. Generated
  custom agent roles retain their independent 272K limits.

## 0.60.2 — 2026-10-18

- Allow owned, non-group/world-writable common receipt parents for lifecycle CLI
  commands while keeping private receipt directories strictly mode 0700.

## 0.60.1 — 2026-10-18

- Preserve printable Unicode device display names in `harness-herdr-web cli devices`
  while retaining control-character filtering and exact UUID selection.

## 0.60.0 — 2026-10-17

- Add explicit device-UUID queries and bounded pane reads through
  `harness-herdr-web cli`, using the existing authenticated HTTPS peer route.
- Add guarded text, keys, commands, agent prompts, and owned resource lifecycle
  actions. Versioned server capabilities and exact target bindings are required;
  private durable intents preserve status and cancellation after response loss.
- Add explicit agent UI answers and context-checked Codex interrupt/exit requests.
  These report observed transport effects, not provider acknowledgement or exit.
- Add managed agent launch preflight and bounded status waiting. Ambiguous effects
  are retained for lookup and are never automatically submitted again.

## 0.59.0 — 2026-10-16

- Add explicit `harness-session report --json` retention diagnostics with optional
  bounded size and managed runtime metadata. The command preserves session state,
  treats unverified provenance and archives conservatively, and never authorizes
  deletion. Installed symlink entry points are covered by regression tests.

## Unreleased — Trusted preservation

- Add the `harness-preserve` trusted preservation command to the next release.
- Add an opt-in, fixed GitHub SSH identity binding for the command's live
  remote query. It disables SSH config and agents, pins host/user/identity,
  and binds safe non-content identity metadata to the native confirmation.

## 0.56.0 — 2026-10-15

Routing changes apply only to profiles with `HARNESS_SESSION_ISOLATION_DEFAULT=1`; other profiles keep their behavior, including in-place restores. The one exception is the `--bg`/`--background` plus continuation-flag reclassification, which also applies in every profile when isolation is chosen explicitly (`--isolated`, `--isolated-session` or `HARNESS_SESSION_ISOLATION=1`).

- A continuation of a session that lives in the canonical harness checkout no longer silently keeps running there. An isolated owner is still restored first, for every spelling. When the owner is the canonical checkout, only the pure Codex forms `codex [profile preset] resume <id>` and `codex [profile preset] --passthrough resume <id>` (presets `fast|base|sol|plan|rich|astra`, kept in the rewrite so the caller's profile still wins over the recorded grant) start a fresh isolated session through durable history; the model and effort are read from the selected history snapshot. Claude, every other Codex form (`-m`, `-s`, `-a`, `-c`, context words, prompt text, or planner argv with `--passthrough`) and a failed history preparation are refused with two quoted, copy-pasteable alternatives: start a fresh isolated session (for Codex that was refused for its form, also `<prefix> codex resume <id>`), or `<prefix> --no-isolated <original args>` for maintenance. Other Codex forms are refused rather than rewritten so a rewrite cannot drop the caller's flags and re-apply a recorded grant.
- Behavior change: a Claude continuation (`--resume/-r`, `--continue/-c`, `--fork-session`, and `--bg` together with one of them) is now owner-checked without a TTY too, and a non-interactive continuation whose owner is not found is refused (it used to take the legacy route). The launcher keywords `resume` and `continue` are refused without a TTY the same way. Fresh runs (`--remote-control`, `--session-id`, `-w/--worktree`, `--tmux`, `--from-pr`, `--bg` alone), client/cloud commands (`attach`, `respawn`, `--teleport`), print runs (`-p`), management commands and non-TTY Codex keep their routes.
- Known limit: non-TTY SDK hosts such as Paseo pass options like `--verbose` before `--resume`. That argv is classified invalid, so the continuation still runs in the canonical checkout, like the non-TTY fresh starts that already do; the harness write guard covers their writes.
- Codex code mode: the peer-checkout guard hook is allowlisted for the PreToolUse adapter, receives the Codex process as its session owner (`HARNESS_HOOK_OWNER_PID`, as in the PostToolUse adapter), and is emitted for `exec|code_mode_exec` next to its direct Bash/Edit/Write/apply_patch entry. Existing Codex homes are not regenerated.
- The bare-launcher menu labels the shared choice as maintenance for which a harness write guard may require a session lease.

## 0.55.0 — 2026-10-15

- Isolated Codex homes inherit natively installed plugins that both the canonical and the session `config/codex-surface.json` list in `runtime_audit.native_codex_plugins`, so a harness audit that requires such a plugin no longer always fails inside an isolated session. Only a plugin the canonical `.harness/codex` has installed and enabled is carried: its single cached version, an exact local `[marketplaces.*]` table and `[plugins."<id>"] enabled = true`. Hook trust, auth, tokens, other runtime state and unlisted plugins are never copied, so plugin hooks stay under native `/hooks` review. Session decisions always win and nothing is replaced or deleted; a native uninstall is not reversed. A missing canonical installation leaves the home unchanged and the audit still fails. Behavior is identical under `HARNESS_CODEX_GLOBAL_PLUGIN_POLICY=manage|preserve`, and `~/.codex` is neither read nor written.

## 0.54.1 — 2026-10-14

- The Kiro authenticated-HTTP bridge runs `mcp-remote@latest` instead of the 0.14.2 pin, so it follows registry releases like the harness package-runner MCPs. `${VAR}` header substitution from the inherited environment was re-verified on 0.14.2 and 0.14.3; the credential still reaches neither argv nor a generated file.

## 0.54.0 — 2026-10-13

- Bare registered prefixes show an isolated/shared workspace choice before fetching, cloning, garbage collection or runtime preparation. Canceling allocates no session; explicit launch and resume controls retain their routes.
- Interactive starts, including managed precreated sessions, skip foreground garbage collection. Explicit GC retains its existing retention and Native history safeguards.
- Resolve source-relative working directories through the verified session owner before launching Claude or Codex. Nested clone directories and the existing shared projects link retain their correct working directory; mismatched owners, missing directories and physical escapes fail closed.
- Add verified explicit Claude fork support for managed launchers. Source leases remain held through exec, original transcripts and owners remain unchanged, and the child does not inherit launcher permission grants. Exact terminal-owner resumes report an actionable fork instruction instead of silently changing ownership.

## 0.53.3 — 2026-10-12

- `protect-pool` protects every profile's Native roots and tombs in the shared old session pool under each record's own source root, instead of refusing the launch with `other profile has Native history`. A recorded source root that is not an absolute normalized path, or that contains the session root, still refuses before any tomb is moved.
- The deletion verifier also accepts Native 0.162.1, characterized with full offline history proofs of real terminal homes.

## 0.53.2 — 2026-10-12

- Classify the top-level entries that long-lived Native Codex homes now contain, so a canonical `codex resume <id>` no longer refuses with `unknown top-level native artifact`. Execpolicy `rules/`, Native app/daemon/tool state and interrupted `mktemp` siblings of generated files are excluded and never copied; generic `sqlite/` and `tui-luna-reserve/` qualify only with their known content, and any other unknown name still holds. Content changes in that state no longer hold a snapshot; an entry appearing or disappearing still does. `external_agent_session_imports.json` is preserved with history.

## 0.53.1 — 2026-10-11

- Normalize process-start timestamp padding at the resume-hold boundary. Web resumes using Python's stripped timestamp now match macOS `ps` output, and previously stored padded holds still protect their original live process. PID reuse checks retain the exact timestamp comparison.

## 0.53.0 — 2026-10-11

- Add explicit Codex command-network choices independently of approval and filesystem access. Workspace-write plans emit exact network booleans; full access permits networking and rejects a conflicting disabled selection. Omitted settings retain Native defaults and preview as unverified.
- Preserve launcher-owned network grants in private records, archives and exact resumes. Nested launches cannot raise network authority. Forks require an explicit choice when the previous workspace policy is unknown.
- Advertise optional network capabilities so Web callers can check an older immutable bound runtime before accepting or preserving a network selection.

## 0.52.0 — 2026-10-10

- Keep source-local Native Codex history outside disposable Git session roots.
  Terminal roots can be retired only after an immutable snapshot, source
  consistency checks and an offline Native restoration proof of complete raw
  rollouts and paginated parent/fork history. Active writers, unknown artifacts
  and uncharacterized Native versions retain their workspaces.
- `codex resume` and `codex resume --list` show the source-local history catalog;
  an exact Native UUID resumes into a fresh local isolation without reopening a
  delivered Git session. Legacy homes and archives remain discoverable. Auth,
  generated settings and permission grants are excluded from history archives.
  Legacy roots without verified inode provenance appear as metadata-only HOLD
  warnings while other verified conversations remain discoverable.
  See [Codex history](docs/codex-history.md) for migration and current limits.

## 0.51.0 — 2026-10-10

- Preserve supported native Codex transcripts and index files in verified private
  archives before terminal session cleanup. Changed or unsafe sources retain
  their worktrees. The `archive` retention mode also stops older numeric-only
  engines before deletion; ordinary headless callers need the selected-release
  dispatcher described in docs/herdr-web-ui.md.
- Add a bounded target-local conversation catalog, exact owner-bound resume and
  explicit archive forks into fresh private homes. Rebuild the selected native
  index from verified raw records and keep imported parent identity separate
  from new session ownership. Authentication and configuration are excluded.
- Add the pure `harness-plan` JSON interface for model, effort, context and
  independent permission controls. Managed Web controls preview and recheck
  these plans while preserving existing runtime policies and fast-start callers.

## 0.50.1 — 2026-10-09

- Read Claude response-collection opt-in from the actual session snapshot,
  retaining the canonical launch-record binding when its checkout is stale.

- Preserve host-optional MCP policies when a server definition is absent:
  explicitly non-required servers are omitted with their policy. Required and
  malformed server policies still fail validation.

## 0.50.0 — 2026-10-09

- Add profile-scoped optional UserPromptSubmit/Stop callbacks for native final
  response collection. Claude launch settings and generated Codex hooks share
  a strict JSON opt-in, preserve existing hooks and fail open when the host
  callback is unavailable. Native Codex hook trust remains required.

## 0.49.4 — 2026-10-09

- Accept explicitly allowlisted Codex connector and templated app IDs alongside
  app IDs. Keep unnamed apps disabled, preserve cold/warm revocation, and reject
  malformed or reserved names before preparation.

## 0.49.3 — 2026-10-09

- Fresh manifest-enabled isolated Codex sessions inherit verified unchanged
  canonical hook approvals instead of prompting for the same hooks again.
  Broker root/inode, snapshot script bytes and exact native hook fingerprints
  gate each entry. Existing disabled/foreign decisions survive; changed or
  unapproved hooks retain native review. No global config or hook-trust bypass
  is used. Native per-profile approvals also survive cold regeneration;
  adding hook trust no longer invalidates the warm surface.

## 0.49.2 — 2026-10-08

- Plugins installed with `claude plugin install <id> --scope project` (or
  `--scope local`) now load in isolated Claude sessions. Claude Code ties those
  install records to the path they were installed for, and an isolated session
  is a separate clone, so such a plugin reported `isn't installed` there; a
  plugin whose marketplace source is a URL or repository (for example the Slack
  plugin) did not load at all. Before an interactive isolated session starts
  Claude Code, the launcher copies the canonical root's records into Claude
  Code's user registry for the session root and removes records for retired
  session roots. Plugin identity is unchanged, so tool names and
  `permissions.ask` rules match the canonical root. The registry is rewritten
  only when needed, under a lock, with a compare-before-replace; a malformed,
  symlinked or hard-linked registry is left untouched with a warning. Headless
  runs are excluded. `HARNESS_CLAUDE_PLUGIN_MIRROR=0` disables it. See
  docs/architecture.md and docs/troubleshooting.md.

## 0.49.1 — 2026-10-08

- `harness-exec --isolated-session <uuid>` now defaults to the session root,
  matching `--isolated`. It no longer injects the canonical caller directory,
  which caused a precreated session's no-argument menu to reject its own working
  directory. Regression coverage uses the real executable and launcher menu,
  and verifies the isolated shortcut default and explicit product cwd.

## 0.49.0 — 2026-10-08

- Codex preparation accepts `HARNESS_CODEX_GLOBAL_PLUGIN_POLICY=preserve` for
  verified session-runtime snapshots. It leaves the global bundled marketplace,
  Chrome cache, native-host configuration and host process intact. A Chrome
  surface must match the existing bridge assets and configuration or preparation
  fails explicitly. The default `manage` policy retains existing behavior.

## 0.48.1 — 2026-10-08

- Headless verifier sandbox, and the headless codex agent sandbox that
  extends it: allow POSIX semaphores named `/mp-*`, so test suites that use
  Python `multiprocessing` locks or events run there. Named semaphores outlive
  a run; a hostile test could exhaust the system-wide table until reboot.
- The "verifier rejected" result now says the sandbox has no network at all,
  not even loopback TCP (it said loopback was allowed).

## 0.48.0 — 2026-10-08

- `harness-headless --target <name> --target-digest <sha256> --task-id <id>
  --approval-sha <sha>` (with `--agent codex` only; with `--agent claude` it
  is `refused`): one Codex run on a registered personal code repository,
  delivered as a draft PR on `loop/<task>-<approval8>`. The launcher clones the
  base branch itself (never the live checkout), prepares the test env with
  `uv sync --frozen --no-build --no-install-project --group <group>` before the
  agent starts, runs the agent under the 0.47 profile plus its path denies
  repeated after the env allows and denies of `~/Library/Keychains`, the
  profile home, the owner's `home_deny` paths, the harness root and the live
  checkout, freezes the session root after the agent (a root replaced by a
  symlink or another directory fails the run), and moves the work through a
  patch and manifest into a launcher-owned trusted.git. `.github/`,
  submodule and secret changes (now also `gh[opsur]_`, `github_pat_` and
  every PEM private key header, in the diff, message and PR body) are
  `refused`; dependency changes (lock files, `uv.toml`, `.python-version`,
  `requirements*.txt`, and the dependency, build and uv tables of
  `pyproject.toml`) and verifier rejections are `failed`. Push, readback and
  `gh` run only in the broker with the gh keyring token in the per-call env,
  journaled `PENDING`/`PUSHED`/`DELIVERED` with resume (bound to the record
  digest), conflict and list-before-create. A target needs GitHub Actions
  disabled. New result status `pr_opened` and fields `target`, `branch`,
  `base_sha`, `pr_url`.
- `harness-profile target add <name> --from <targets.yaml>` (needs a TTY,
  shows the entry and its diff, asks for the name again; the live path must be
  absolute and is stored as its realpath), `target list` and `target show`.
  The owner's account, SSH aliases and deny lists come from a required
  `target-policy.json` in the profile home. Shared digest vectors:
  `test/fixtures/target-digest-vectors.json`.
- Security: isolated sessions record their root's inode at `create`, and
  `submit`, `close` and `exit` refuse (exit 10) to stage a root that was
  replaced by a symlink or another directory; headless runs report it as
  `refused`.
- Security: the headless Claude lane's sandbox and edit tools now deny writes
  to the profile home in use (`~/.config/harness-launcher` by default).

## 0.47.0 — 2026-10-08

- Security: the headless repository verifier's Seatbelt profile let a
  sandboxed test read the argv and environment of processes outside the
  sandbox (`KERN_PROCARGS2`, `KERN_PROC_PID`; measured on macOS 26 against the
  broker that starts it): its same-sandbox process-info allow did not deny
  the rest. The profile now ends with `(deny process-info*)` and `(allow
  process-info* (target same-sandbox))`, and the verifier breach test probes
  both sysctls against the broker pid.
- Security: the headless repository verifier could read every `/dev/ttys*`
  terminal the user had open (the profile allowed all of `/dev`) and, when
  `harness-headless` ran from a terminal, open the operator's terminal as
  `/dev/tty`. Its profile now reads only `/dev` itself and a fixed device
  list, and the launcher runs the broker in a new session with no controlling
  terminal.
- `harness-headless --agent codex`: one unattended Codex run in the same
  isolated session, delivery and result contract as Claude. The launcher
  creates the session itself (headless clone, lease, heartbeat), writes a
  fresh 0700 `CODEX_HOME` with only `config.toml` (a `loop` provider on the
  Responses API, `approval_policy = "never"`, `sandbox_mode =
  "danger-full-access"`, history off, no MCP servers, hooks, notify or project
  trust) and `AGENTS.md` (the clone's `.claude/rules/*.md`), and runs `codex
  exec --json -o` under a deny-default Seatbelt profile from the verifier
  family: reads of toolchains, the session root and the run's temp directory,
  writes only there, the verifier's fixed `/dev` list, PTYs only through the sandbox PTY
  extension (never the user's open terminals), no inspection of outside
  processes (`KERN_PROCARGS2` included), TCP only to the forwarder's exact
  loopback port, and denies after every allow for credential stores,
  `~/.codex`, `~/.claude`, `~/.hermes`, `~/.config/harness-launcher`, the
  source checkout and other sessions. Model requests go through a per-run
  forwarder thread of the launcher, the only holder of the endpoint key:
  exact `Host`, no `Origin`, a per-run token from `config.toml`, only `POST
  /v1/responses` and `GET /v1/models`, the configured model only (duplicate
  JSON keys refused), an allowlist of request headers with the key added, no
  redirects, unbuffered SSE, at most 8 requests at once, a request cap
  (`--max-model-requests`, default 400, over it the status is `budget`), `::1`
  at its port held by the forwarder, and no logging. A signal kills
  processes left under the session root or temp directory before the session
  is finished, on the Claude path too. New options `--agent`, `--effort` (per agent),
  `--model-endpoint` (`http://127.0.0.1:<port>/v1` only),
  `--endpoint-key-file` (regular 0600 file of the user with one link, no
  symlink, unreadable inside the sandbox) and `--max-model-requests`. Preflight refuses before any
  model request when codex, the key, the forwarder, `GET /v1/models` or
  `codex --version` or `codex-code-mode-host --help` under the profile
  fails. Code-mode models (gpt-6.1-sol) need the `codex-code-mode-host` that
  ships beside the npm vendor codex binary: the launcher requires it next to
  the resolved binary, refuses either file when it is not owned by the user
  or root or is group- or other-writable, and the sandbox allows exactly that file; point
  `HARNESS_CODEX_BIN` at the vendor binary. Results add an optional
  `usage: {input_tokens, output_tokens}`; `cost_usd` is null for Codex.
- `harness-headless --effort` for Claude sets the launcher-owned
  `effortLevel` (`low`, `medium`, `high`, `xhigh`; `xhigh` also turns thinking
  on). Calls without the new options are unchanged.

## 0.46.2 — 2026-10-08

- herdr plugin: a turn that ends in a pane out of sight is now treated as
  finished. herdr reports it as `done` until the user sees the pane, and only
  `working` → `idle` counted, so in a background tab neither the response
  notification nor the `$decision` (`결정 필요`) token appeared. `done` now counts
  like `idle`. The `idle` herdr reports on the pane's next change after the user
  has seen it is the same finished turn: it neither notifies again, nor drops a
  notification held for background work, nor clears the token.

## 0.46.1 — 2026-10-08

- Herdr desktop notifications defer an ended response while its background
  activity is unresolved. Expired observations stay uncertain; only an explicit
  settlement for the same activity cycle releases the notification. New turns
  and session identities cancel the old deferred response. Notifications prefer
  a manual pane label and session metadata title, and say “응답 종료” rather than
  claiming the whole task is complete.

## 0.46.0 — 2026-10-08

- herdr plugin: a Claude turn whose last answer marks a recommended option
  (`← 추천`) now notifies `🔘 claude 결정 필요` instead of `완료`, and sets a
  `$decision` pane token to `결정 필요` until the next non-idle status or a turn
  without the mark. Add `{ token = "$decision" }` to a sidebar row to show it
  (docs/terminal-runtimes.md).

## 0.45.0 — 2026-10-07

- Headless deliveries carry the agent's commit message. The broker delivers
  the work tree as one commit, so the agent's own commits and their messages
  were lost and every delivery read `harness session <uuid>`.
  `harness-headless` now asks the agent (a delivery note appended to the
  prompt) to write the message to `$HARNESS_COMMIT_MESSAGE_FILE` in its
  private temp directory. The launcher reads it without following symlinks or
  blocking, only as a regular single-link file of at most 8192 bytes of strict
  UTF-8, treats U+2028 and U+2029 as line breaks, removes control and Unicode
  format characters (except LF and TAB), CI-skip directives (`[skip ci]` and
  its variants) and any `Harness-Session` or `skip-checks` trailer line,
  including spoofed spellings (full-width, spaced, case variants), and accepts
  it only with a non-empty subject of at most 100 characters and at most 200
  lines. The message is copied into the launcher-owned session record only
  while the broker closes a session with changes, and the broker commits with
  it plus a `Harness-Session: <uuid>` trailer; every outcome other than
  `delivered` leaves no message in the record (short of a SIGKILL during
  `close`). Anything else falls back to the
  generic message; the run log names which was used. The agent's `.git` is
  never read, and the result file is unchanged (version 1).
- New `harness-session discard <uuid>` retires an `ABANDONED` or `CONFLICT`
  session whose work will not be delivered; before, such a session could only
  be reopened and was never collected. It takes the session's runtime lease
  without waiting (a live owner refuses), first writes the work tree as a
  binary patch against the session base and `discarded-at` into the record
  (flushed with `F_FULLFSYNC`). The patch is staged from a copy of the index
  submit would use: it holds the tracked content as that index tracks it
  (force-added ignored files kept, `git rm --cached` removals kept) plus
  untracked, non-ignored files; other gitignored files and machine-local paths
  are left out. It refuses and leaves the journal unchanged
  if that patch cannot be written completely, and then moves the session to
  the new terminal state `DISCARDED`. `gc` retires `DISCARDED` work trees
  after the retention period, like `CLOSED` and `DELIVERED`; `resume` and
  `recover` refuse them. Other states refuse with exit 2: `OPEN` (use `exit`
  or `close`), `SUBMITTED` and `INTEGRATING` (use `recover`), `CLOSED`,
  `DELIVERED` and `DISCARDED`. `DISCARDED` is reachable only through
  `discard`: `harness-session transition <uuid> DISCARDED` refuses, and
  reopening a session removes any leftover discard evidence.

## 0.44.1 — 2026-10-07

- The headless verifier sandbox denies by default. The 0.44.0 profile started
  from `(allow default)`, so reads outside `HOME` (`/private/etc`,
  `/private/var/folders`, the Homebrew `etc` and `var`), the `sysctl`
  process table and exec of any binary were open. The profile now allows
  only what the repository verifier and its tests were observed to need:
  fork and exec under system and toolchain prefixes and the verifier's own
  dirs; reads of system and toolchain trees, a few `/private/etc` files and
  the Homebrew OpenSSL and CA config; file metadata; `sysctl` reads except
  `kern.proc`; plus the 0.44.0 network, write, mach-service, signal and
  credential rules. The state home is denied explicitly (so a `PATH`
  directory that holds it does not expose other sessions), `/Library` reads
  are limited to its toolchain parts, and `PF_SYSTEM` kernel-control sockets
  are no longer allowed.

## 0.44.0 — 2026-10-07

- `harness-headless` runs the repository verifier in a sandbox. The verifier
  runs the candidate's tests, which the agent may have written, and it ran
  them as the user with network, `~/.ssh`, the keychain and git credentials
  reachable. For headless sessions it now runs under `/usr/bin/sandbox-exec`
  with a launcher-generated Seatbelt profile, on a throwaway copy of the
  candidate the broker never reads back, with a minimal environment: no
  network (not even loopback), writes only to the copy and a per-verify temp
  dir outside `HOME`, nothing under `HOME` or the state home readable beyond
  toolchains (no user git config, git credential stores or `.netrc`; a
  generated identity-only git config instead; no remote URLs in any clone it
  can read), no
  mach services beyond user lookup and logging (no keychain, launchd,
  LaunchServices or AppleEvents), and no signals to processes outside the
  sandbox. The broker commits and pushes from the untouched candidate without
  hooks, fsmonitor or drivers. A host without `sandbox-exec`, or where the profile
  does not load, is refused before Claude starts. Interactive sessions are
  unchanged.
- A headless verifier rejection now ends `failed` (broker exit 9) instead of
  `conflict`; `conflict` is left to merge conflicts and a remote that keeps
  moving.
- A source checkout that has moved or been deleted has its own broker exit
  code (8) and `harness-headless` reason, instead of reading as a headless
  record without its trusted git dir.

## 0.43.1 — 2026-10-07

- `harness-headless` and `harness-session close` no longer fail when the
  harness `.gitignore` ignores the machine-local files the session holds
  (`.claude/settings.local.json`, `mcp.local.json`, and the like). Staging
  named those paths in an exclude pathspec, and `git add -A` exits 1 when a
  pathspec names an ignored file, so every headless run in such a harness
  ended `failed` after the agent had finished, and interactive `close`
  stopped the same way. Staging now adds the whole work tree and then resets
  the excluded paths: headless sessions reset them to the base (their
  launcher-owned git dir also ignores them), so an edit to a tracked file
  under an excluded path is not delivered; interactive sessions unstage them
  as before and still refuse excluded paths they committed. Both kinds of
  session list the excluded paths in their own `info/exclude`, so staging
  never reads inside them. The excluded-path fence now matches case
  variants (`Projects/`, `config/.LOCAL/`, `MCP.local.json`), and names the
  filesystem folds onto an excluded entry even where git does not (on APFS,
  `projectſ` with U+017F is `projects`), so a session that negates the
  ignore rules still cannot deliver them; an interactive session that
  committed one is refused. A real staging failure elsewhere, such as a nested
  repository with no commit or an unreadable file, still ends `failed`.
- Headless session creation no longer fails when the user's git config sets
  a hooks-only `init.templateDir`; the launcher-owned git dir is created
  without user templates or config.
- Session delivery fails closed when a broker git call fails. Before, a
  failed listing of staged paths read as "nothing to exclude" (a Unicode
  alias of `projects/` could be delivered), a failed change check read as "no
  changes" (the session was closed and its work reported as `no_changes`), a
  failed check for committed excluded paths let an interactive submission
  through, and a failed tree listing after the commit was read as deletions
  and pushed before the readback reported a conflict. Each of these now
  stops the close, keeps the session, and delivers nothing; `harness-headless`
  reports `failed`. A headless clone whose `projects` link cannot be removed
  is refused.
- The filesystem alias check probes the source checkout's volume as well as
  the session volume, so a state home on a case-sensitive volume no longer
  lets a name the source volume folds onto `projects/` through. A delivered
  path that looks like pathspec magic (`:name`) is read back as itself
  instead of leaving the session stuck in `INTEGRATING`, and inherited git
  pathspec and git-dir variables no longer reach broker git.

## 0.43.0 — 2026-10-07

- New `harness-headless` runs one unattended Claude task in a fresh isolated
  session and writes one JSON result (`delivered`, `no_changes`, `conflict`,
  `failed`, `timeout`, `budget`, or `refused`). It keeps only an environment
  allowlist, enforces a launcher-owned sandbox (strict network allowlist,
  read denies for credential directories and the source root) and permission
  denies, accepts only additive caller settings, kills the process group
  on timeout, holds a caller lock for its whole run, and delivers a successful
  change through `harness-session close`. See docs/architecture.md.
- `harness-exec <harness> --isolated ...` no longer injects the current
  directory as `--cwd`. A non-interactive `--isolated -p` run now works in the
  session root instead of the canonical checkout. Other routes are unchanged.
- Headless isolated sessions (`HARNESS_HEADLESS=1`) clone without hardlinks,
  copy machine-local MCP and settings files instead of linking them, drop the
  settings `env` block, never link `projects/`, refuse a clone with a symlink
  on those paths, and keep a launcher-owned git dir in the session record.
  Broker git on a headless session uses only that git dir with the session
  root as work tree, never the session's own `.git`, so its config, hooks,
  modules and alternates cannot run anything. Before delivery,
  `harness-headless` kills processes left with a cwd or open file in the
  session root.
- Isolated sessions with commits past their base now count as changed, so
  committed work is delivered instead of being closed as clean. An
  interactive submission whose commits touch an excluded machine-local path (`config/.local`,
  `projects`, `.mcp.local.json`, `mcp.local.json`, `.claude/settings.local.json`)
  is refused with exit 7; such paths that are only staged are unstaged. Broker
  git on a session root disables fsmonitor, hooks, submodule recursion, the
  untracked cache and external diff drivers.

## 0.42.0 — 2026-10-01

- herdr: a tab that holds several agents now shows the first agent's session
  title followed by the number of other agents, such as `release notes +2`.
  Before, the plugin labeled only tabs with exactly one pane, so a tab went
  back to its position number as soon as it was split or another agent was
  moved into it. A plain shell beside an agent no longer removes the title
  either. The count stays inside the 20-cell label width. Tabs you named
  yourself are still left alone.

## 0.41.1 — 2026-10-01

- Codex sessions that start together no longer fail with `timed out waiting
  for Codex home preparation lock`. Preparation of one generated home runs one
  session at a time, and the wait for it was fixed at 20 seconds: a terminal
  multiplexer restoring eight Codex panes after a reboot lost two of them. The
  wait is now 300 seconds, prints one notice after 5 seconds, and can be set
  by exporting `HARNESS_CODEX_HOME_LOCK_TIMEOUT` (seconds, capped at 3600) in
  the environment that starts the sessions. The global cache lock under
  `~/.codex` uses the same limit. A caller that relied on the 20-second
  failure to stay non-blocking should export a small value. See
  docs/troubleshooting.md.

## 0.41.0 — 2026-10-01

- `harness-herdr-web usage --watch` draws Claude and Codex plan usage as a live
  board for a herdr popup (`[[keys.command]]` with `type = "popup"`): per
  window a bar of the used share with a tick at the share of the window
  elapsed, the pace, the reset time and the time left, plus a red line with
  the expected exhaustion time or a yellow line with the projected share. It
  reads again every 60 seconds or on `r`, keeps the last board when a reading
  fails, and closes on `q`, `Esc` or `Ctrl-C` (`ㄱ` and `ㅂ` work under the
  Korean input method). Text from the server is stripped of control
  characters before it reaches the terminal. See docs/herdr-web-ui.md.
- herdr: the plugin no longer reports a `$quota` token on each Claude and Codex
  pane. Plan usage belongs to the account, so the sidebar repeated one value on
  every agent row; the tab bar line and the popup board show it once. Tokens
  that 0.40.0 reported expire within 15 minutes; remove `$quota` from
  `[ui.sidebar.agents]` rows copied from the 0.40.0 example.
- Usage windows that differ only in scope (such as a per-model weekly limit)
  keep separate pace history and show the scope in their label.

## 0.40.0 — 2026-10-01

- herdr: a session rename now shows within about a second. Before, the plugin
  ran only on status, focus and pane/tab lifecycle events, so a rename made
  during a long turn showed when the turn ended, and herdr's sidebar kept the
  Codex terminal title (often the thread id). The plugin now starts one
  background watcher that subscribes to `pane.updated` over herdr's API socket
  and checks the Codex session index and Claude transcripts behind the current
  titles once a second. One watcher runs per herdr session. It exits when the
  plugin file changes (an upgrade), the plugin is disabled or unlinked, or
  herdr stops answering; `HARNESS_HERDR_WATCH=0` keeps it from starting.
- herdr: Claude titles come from the transcript's latest custom title first. A
  title set by a harness title hook reaches Claude's terminal title only at the
  next prompt.
- herdr: each agent pane reports its full title as herdr's metadata title (the
  sidebar `pane` token), Korean state labels (`state_text`), and a `$model`
  token with the model and effort of the latest turn, again after a herdr
  restart. Every five minutes the watcher adds a `$quota` token with that
  agent's plan usage. See the sidebar example in docs/terminal-runtimes.md.
- herdr: Codex's blinking `[ . ] Action Required |` marker no longer becomes a
  tab label, and the `tab.renamed` event of the plugin's own rename no longer
  runs a sync.
- `harness-herdr-web usage` prints Claude and Codex plan usage for herdr's tab
  bar: per window the used share, the burn pace against an even spend, the
  expected exhaustion time when the current rate runs out before the reset,
  and the reset. The provider badge is an emoji, because herdr strips colors
  from status commands. It never follows a redirect with the token.
  `harness-herdr-web` now calls herdr through `HERDR_BIN_PATH` when set, so it
  works from herdr's server `PATH`.

## 0.39.5 — 2026-10-01

- Fix: a Slack-enabled Codex session launched or resumed with `bypass` asked for
  approval on every MCP tool call, such as a read through a company MCP server.
  0.39.3 turned bypass into `danger-full-access` with `on-request` approval so
  Slack mutations prompt, but Codex auto-approves MCP prompts only under
  `never` with full disk access. A final `danger-full-access` launch now sets
  `default_tools_approval_mode = "approve"` for each MCP server and each enabled
  non-Slack app in the generated home. Slack apps and Slack-named MCP servers
  keep their policy and their mutations still prompt; modes already set in the
  home or by the caller win. The launch record is unchanged, so restoring a
  recorded `on-request` + `danger-full-access` session applies the same rule.
- Codex `sol` now uses GPT-6.1 Sol at high effort (was medium) and `rich` at
  xhigh (was high), one step apart like Claude's `opus` and `rich`. OpenAI's
  GPT-6.1 Sol announcement (2026-10-01) puts high above medium on every published benchmark
  (DeepSWE 75.2 vs 73.0) and xhigh ahead of high on long agentic workflows
  (AutomationBench 35.5 vs 33.2), though below it on DeepSWE (71.9).
  Subagent tiers are unchanged.

## 0.39.4 — 2026-10-01

- Fix: after each upgrade, every Codex launch asked to review the launcher's hooks
  (`20 hooks are new or changed`). A resumed session typed in a shell (the
  herdr/Orca restore route through `harness-auto`) entered the package through
  its versioned `Cellar/harness-launcher/<version>` path, and generated hook
  commands kept that path, so an upgrade changed every command. Until trusted,
  the hooks did not run, and an unattended restore stopped at the prompt. The
  same path in a Claude session's launch-record hook broke once `brew cleanup`
  removed the old keg. `harness_launcher_stable_dir` now maps the keg, the
  compat `share` link and `opt` to the `opt` path when they are the same
  directory, for generated Codex hooks, the picker and the shortcut path.
  A home last prepared through the keg changes its hook commands once more on
  the next launch; trust them once (a harness autotrust or `t` in the review).
  Sessions already running keep hooks that name the previous keg: upgrade with
  `HOMEBREW_NO_INSTALL_CLEANUP=1` and run `brew cleanup` after they restart.

## 0.39.3 — 2026-10-01

- Require separate native user approval for public Slack mutations across
  Claude and explicitly opted-in Codex connectors, including bypass/never,
  resume, final caller overrides, and guarded stdio app-server turns. Preserve
  reads, reactions, drafts, existing Claude settings/hooks, and filesystem
  grants. Private connector ids remain harness-owned configuration. Reject
  Happy wrappers for Slack-enabled harnesses until their internal runtime can
  preserve native user approval.

## 0.39.2 — 2026-10-01

- herdr plugin: a Codex tab now takes its thread's latest name. A running Codex
  never updates its terminal title when a harness hook renames the thread from
  another app-server connection, so the tab kept the thread id (shown as its
  position) or the name the session resumed with. The plugin reads the name
  from the harness `.harness/codex/session_index.jsonl` found from the pane's
  directory upward, then `~/.codex`, and falls back to the terminal title.
  Notifications use the same title, without the Codex `| <name>harness` suffix.
  herdr's own sidebar still shows the terminal title.

## 0.39.1 — 2026-10-01

- Fix: sessions started from the interactive picker (`<prefix>` with no
  arguments, `harness-exec <dir>`) wrote no launch record, so after a terminal
  host restart they came back without their permission mode, Codex profile or
  1M context. The picker now passes the same launch-record hook to Claude and
  exports the same `HARNESS_LAUNCH_*` to Codex as the shortcut path; both paths
  share one implementation in `harness-common.sh`. Sessions started from the
  picker before this release still have no record; relaunch them once as the
  restore hint says.

## 0.39.0 — 2026-09-30

- New `harness-herdr-web` (`install`, `check`, `configure`, `token --copy`) runs
  [herdr web ui](docs/herdr-web-ui.md) `v0.3.34` local-only behind a token. It
  enforces an effective `HERDR_WEB_TOKEN` of at least 32 characters, `HOST=127.0.0.1`
  and manual updates across the plugin's `env` and `.env`, and refuses symlinked
  or foreign-owned files. It probes the running server with an unauthenticated
  `/api/health?scope=bridge`, warns on a checkout that is not the pinned commit,
  and fails while Tailscale reports a login for the machine, an unsupported
  configuration for now.

## 0.38.1 — 2026-09-30

- Fix: restoring a long Claude session dropped the `[1m]` suffix, so a 1M session
  came back at 200k context and compacted at once. The old rule needed a
  `cost-state` record inside the 8 MiB transcript window, which a long session can
  lack. `[1m]` is now also added when a main-thread turn in the window used more
  than 200000 prompt tokens, or when the launch record says the launcher started
  the session with a `[1m]` model (`context=1m`, a non-grant field passed to the
  hook as `--context 1m`). Sessions launched before this release have only the
  usage signal.

## 0.38.0 — 2026-09-30

- Restore fidelity. A typed `claude --resume <id>` or `codex resume <id>` (herdr,
  Orca, `<prefix> --resume <id>`) now keeps the session's model and effort, read
  from its transcript or rollout, instead of resetting to the `base` defaults.
  Only a pure resume of one UUID with no caller `--model`, `--effort`,
  `--permission-mode` (Claude) or `-m`, `-p`, `-a`, `-s`, `--full-auto`, bypass
  flag (Codex) restores; shell routing marks its host-default `base` with
  `HARNESS_HOST_DEFAULT_MODE=base`, which the launcher consumes before any agent
  starts. A restored Claude session always launches directly, so
  `<prefix> --resume <id>` no longer drops the id into the picker.
- Permission and sandbox are restored only from a launcher-owned launch record
  (`<state>/launch-records/<agent>-<session_id>`), never from a transcript or
  rollout. Claude gets the `SessionStart` hook through the launcher's `--settings`,
  merged with forced thinking into one JSON; Codex gets it through the new
  `launch_record` registry row (`HARNESS_LAUNCH_RECORD_HOOKS=1`, opt in per
  harness and rerun the Codex hook trust step). A restore without a record
  keeps defaults and prints the one command that relaunches with bypass. The Claude hook gets its grant as command
  arguments, never from the environment (Claude applies `settings.local.json`'s
  `env`); a launch nested inside an agent (decided by the hook from process ancestry) can lower a record but never raise it, with `dontAsk` ranked just above `plan`; a
  Codex restore reapplies the recorded launcher profile (for example `plan`, which is
  read-only) while it still exists. The record reader also requires an owned,
  single-link file under an `O_NOFOLLOW` directory, and the probe reads exactly its
  8 MiB window and ignores non-string values.
- In an isolation-default profile, a restore of a session that lives in the
  canonical harness root is no longer rejected: a Codex rollout found only in the
  source `CODEX_HOME`, or a Claude session with a launch record `isolated=0`, takes
  the legacy route. Forced isolation still rejects.
- New commands `harness-restore-probe` and `harness-launch-record`.
- Behavior change: the launcher's Claude `--settings` argument now always carries
  the launch-record hook, so it is never the bare
  `{"alwaysThinkingEnabled":true}` string.

## 0.37.5 — 2026-09-30

- Fix 0.37.4, which broke every Codex launch in a harness with an exact
  Codex surface. The folder trust tables it adds to `config.toml` failed the
  surface warm check (`projects` was not an allowed root key), so
  `codex-home-prepare.sh` exited 3. The warm check now accepts `projects`, the
  managed-config fingerprint ignores it as runtime state, and the late runtime
  merge keeps trust that Codex saved without duplicating the launcher's own
  roots.

## 0.37.4 — 2026-09-30

- Codex no longer asks to trust the harness folder on every launch. The
  launcher rewrites `$CODEX_HOME/config.toml` on each launch, and it dropped the
  `[projects."<path>"]` tables where Codex saves folder trust. It now keeps
  those tables and writes a trusted entry for its own roots only: the harness
  root and, in an isolated session, the source and session roots, keyed by
  physical path. A decision you already made for one of those roots wins.
  Nested repositories under the harness are not trusted automatically.

## 0.37.3 — 2026-09-30

- Codex presets move to the latest models. `fast` and the `haiku` subagent
  role use GPT-6 Luna; `sol`, `plan`, `rich` and the `opus` role use GPT-6.1
  Sol. The opt-in `luna6` and `sol6` profiles are removed; use `fast` and
  `sol` instead. The default profile stays on Terra.

## 0.37.2 — 2026-09-30

- herdr plugin: notifications now use `terminal-notifier` when it is installed
  with Homebrew. herdr runs plugins with only the system `PATH`, and the
  plugin's Homebrew lookup never matched the resolved Cellar path of herdr, so
  every notification fell back to `osascript`. It showed as Script Editor, and
  clicking it opened Script Editor instead of focusing the agent pane.

## 0.37.1 — 2026-09-29

- herdr plugin: new `install` command. herdr records a linked manifest by its
  resolved path, so linking the packaged directory as 0.37.0 documented pinned
  the versioned Homebrew Cellar path, which the next upgrade removes.
  `harness_herdr_plugin.py install` writes the manifest outside the package,
  runs the script through the unversioned `opt` path, and links it. Re-run it
  once if you linked the plugin under 0.37.0.

## 0.37.0 — 2026-09-29

- New herdr plugin in `share/harness-launcher/herdr-plugin` (macOS, herdr
  0.9.1+). It labels each single-agent tab with the agent's session title, cut
  to 20 display cells, and leaves tabs you named alone. It replaces herdr's
  popups with notifications that carry the session title and workspace, and
  clicking one activates the host terminal app and focuses the agent pane.
  Link it with `herdr plugin link` and set `ui.toast.delivery = "off"`. See
  `docs/terminal-runtimes.md`.

## 0.36.0 — 2026-09-29

- Terminal runtime parity. Each launch detects one runtime, in the order
  `herdr > orca > cmux > plain`, and a launch without a terminal (stdin or
  stdout not a TTY) is `plain`. It removes every other runtime's variables
  (`CMUX_*`, `ORCA_*`, `TERM_PROGRAM=Orca`, `HERDR_*`, and Orca's `CODEX_HOME`)
  and exports `HARNESS_TERMINAL_RUNTIME`. cmux title brokers now start only in
  cmux. See `docs/terminal-runtimes.md`.
- herdr: the launcher reports its run directory to herdr as an `OSC 7`
  sequence so a pane restore is typed in the right directory, and reports the
  caller's directory again once the agent or the interactive launcher returns.
- Codex runtime hook registry: the Orca-only hook is now one row of a registry.
  New opt-in `HARNESS_HERDR_AGENT_HOOKS=1` in `config/launcher.env` adds a
  `SessionStart` status row for herdr. Both rows are status-only and fail-open
  (output discarded, always exit 0). `orca_hooks_optin.py` remains as a
  compatibility wrapper for the new `runtime_hooks_optin.py`.
- New `harness-session-provider-record` records Claude session ids (including
  `/clear`, `compact` and `fork` ids) in a session's `provider-sessions` file,
  for harnesses that register it as a `SessionStart` hook.
- Restore routing counts the session directory and `provider-sessions` owners
  together. A recorded id maps only when its transcript exists; two or more
  owners is ambiguous. The launcher records each isolated session's run
  directory (the caller's directory inside the harness that `harness-exec`
  passes as `--cwd`) in the session's `run-dirs`, and the transcript check
  looks under the session root and every recorded run directory inside the
  session's source or session root, so `/clear` ids map from the harness root
  and its subdirectories.
- Documentation: new `docs/terminal-runtimes.md` (also installed with the
  package) with host prerequisites for herdr and Oh My Zsh.

## 0.35.0 — 2026-09-29

- Codex through SDK hosts. New `harness-codex` executable: `--version`/`-V`
  run native Codex (host feature probes), `--profile <name>` runs
  `harness-profile <name> codex --passthrough …`, and without a profile the
  current directory selects the harness.
- Codex `--passthrough`: later tokens are native Codex arguments. A caller
  `-p`/`--profile` replaces the launcher profile, and a caller `-C`/`--cd` in
  any form replaces the launcher `--cd` after it is validated to stay inside
  the harness (exit 2 otherwise).
- The launcher no longer passes `-p <profile>` to Codex subcommands that
  reject it (`app-server`, `login`, `features`, `debug` except
  `debug prompt-input`, …); an explicit launcher profile with such a
  subcommand exits 2. A test compares the table with the installed `codex`.
- A plain `codex app-server` runs behind `codex-app-server-guard.py`, a
  JSON-RPC relay that rejects thread and command working directories outside
  the selected harness (or inside another registered harness nested in it) and
  pins `thread/resume`/`thread/fork` without `cwd` to the server directory.
  `app-server daemon`/`proxy`, non-stdio `--listen`, `remote-control`,
  `exec-server`, and `mcp-server` exit 2 through the launcher (use `command codex`). A schema
  drift test classifies every client-sent path field of the installed Codex.
- New `harness-paseo print|sync|check` manages `harness-claude` and one
  `harness-codex-<profile>` Paseo provider per registered profile, owning only
  the IDs and fields it wrote (recorded in
  `~/.config/harness-launcher/paseo-managed.json`), with a 0600 backup and a
  concurrent-change abort.
- Claude `--passthrough`: a management subcommand right after the marker
  (`auth status`) runs natively; an explicit caller thinking disable drops a
  launcher `xhigh`/`max` effort; a caller `--` ends option scanning.
- A launcher `--` now ends keyword parsing: it and every later token are
  forwarded last, after launcher flags, and imply a direct launch.
- Isolated sessions are finished (heartbeat stopped, session closed) on every
  exit after acquisition, including error returns.
- `harness-auto` validates attached `-C<dir>` and `-C=<dir>` Codex forms.
- Breaking: with `harness_shell_enable`, plain `claude` and `codex` keep native
  argv (`harness-auto <runtime> --passthrough "$@"`), so `claude rich` passes
  `rich` as a prompt; use `<prefix> rich` for presets. `codex --version`,
  `-V`, and `--help` run native Codex.
- Packaging: the formula installs and links `harness-codex` and
  `harness-paseo`, plus `codex-app-server-guard.py` and `harness_paseo.py`.

## 0.34.0 — 2026-09-29

- Add the `--passthrough` launcher marker for SDK hosts such as Paseo. Claude
  arguments after it are forwarded verbatim instead of being read as launcher
  keywords (`--permission-mode plan` no longer becomes the `opusplan` preset),
  and an explicit caller `--model`, `--effort`, or `--permission-mode` replaces
  the launcher default. Launcher-owned flags stay ahead of the caller argv, and
  a launcher `continue`/`resume` keyword combined with a caller session flag
  fails with exit 2. Invocations without the marker are unchanged. See
  `docs/paseo-integration.md`.
- Packaging: the Homebrew formula installs `docs/paseo-integration.md` under
  `pkgshare/docs`, next to `orca-integration.md`.

## 0.33.0 — 2026-09-29

- Orca main-host support. When Orca injects `CODEX_HOME` (equal to
  `ORCA_CODEX_HOME`), launcher entry points drop both variables so Claude and
  other launcher-owned runtimes do not inherit Orca's managed Codex home;
  a different user-set `CODEX_HOME` is kept. `checkup prompt-audit` also drops
  `ORCA_*`.
- The cmux title brokers no longer start inside an Orca terminal (stale
  `CMUX_*` variables are ignored), and the Codex title hook is a no-op there.
- Add `HARNESS_ORCA_AGENT_HOOKS=1` (in a harness's `config/launcher.env`) to
  append Orca's status-only, fail-open Codex hook (output discarded, always
  exit 0) for six events to the generated `hooks.json`. The prepare script
  reads the opt-in only from that file, so launcher and direct callers agree;
  the process environment is ignored. The opt-in is part of the warm-path
  fingerprint; without it the output is unchanged. Rerun the harness's Codex
  hook trust step after changing it.
- Isolation-default profiles now accept Orca's restore argv: fresh isolated
  Claude launches on the isolate route carry `--session-id <session UUID>`, and `--resume <id>` /
  `codex resume <id>` map to the owning isolated session for the current
  harness. Unknown, ambiguous, delivered, retired, and leased sessions fail
  with specific messages. Restores after Claude `/clear` remain unsupported.
- `docs/orca-integration.md` documents the hook policy, the recommended worktree
  base (`<harness>/projects/<repo>/.worktrees`), profile relaunch behavior,
  `CODEX_HOME` sanitization, and resume behavior. Package it under
  `pkgshare/docs` in the Homebrew formula.
- Packaging: the Homebrew formula must also install the new
  `bin/orca_hooks_optin.py` next to `codex-surface.py`. The warm probe,
  fingerprint, and prepare script import it, so a missing module makes every
  Codex home preparation fail.

## Unreleased

- `docs/orca-integration.md` no longer recommends one Orca profile per trust
  boundary. Orca profiles share one PTY daemon and readable terminal history on
  a macOS account, so they are not a security boundary. The guide now documents
  the hidden multi-profile UI flag, profile defaults, and relaunch behavior.
- Add `<prefix> checkup prompt-audit` to run Claude Code's `/checkup
  prompt-audit` headless at the harness root in restricted, read-only mode with
  a budget cap, and `harness-profile checkup prompt-audit (--all | <prefix>...)`
  to run it for several profiles in turn. Reports stay in each harness under
  `.harness/reports/checkup/`; the terminal shows only a status line.
  `checkup` and `register` become reserved profile prefixes. `<prefix> checkup
  ...` previously started Claude with `checkup ...` as the prompt; it now runs
  the audit instead.

- Shorten the Codex surface test stage by overlapping its reviewed serial
  safety group with three isolated private-fixture shards. New tests still run
  through a serial gate, and the original one-job route remains available.

- Add native Codex `luna6` and `sol6` opt-in profiles for GPT-6 Luna/low and
  GPT-6 Sol/medium. Keep existing defaults, profiles, and subagent routing.

- Pass each profile's existing local MCP credentials to Claude sessions launched
  from nested projects, including direct, light, TUI, and isolated routes. Keep
  credentials scoped to the Claude child and suppress shell tracing while
  loading them; no token rotation or generated-config secret copy is needed.

- Treat each profile's MCP JSON as the definition SSOT and materialize explicit
  `${HARNESS_ROOT}/...` stdio script paths once for Claude, Codex, and Kiro.
  Nested project/worktree launches now use the same checked absolute path;
  bare relative interpreter scripts fail with a migration hint. Existing
  machine-local overlays should use the explicit prefix.

- Route plain-shell Claude management commands such as `mcp`, `auth`,
  `plugin`, and `doctor` directly to the native CLI after location-based
  profile selection. Their argv, project PWD, and local environment are kept,
  while session-only model/effort, MCP overlay, title, observability, and
  isolation mutations are bypassed; prompt-shaped inputs stay on the normal
  harness session route.
- Skip Claude Code's auxiliary AI session-title request for eligible new
  native-direct interactive launches by using a one-shot, exact-session
  bootstrap name while preserving resume, remote, hook-free, and manual-title
  paths.

### Added

- Add opt-in `harness_shell_enable` / `harness_shell_disable` routing for plain
  interactive-Zsh `codex` and `claude` commands. Selection uses the existing
  registered-profile resolver and fails closed outside a boundary; Codex
  `--cd`/`-C` cannot escape the selected harness, while `command codex` and
  `command claude` remain explicit native escape hatches.

- Add granular native-Kiro selection. The TUI mode menu ends with `🔧 Custom`,
  which lists the runtime's live model catalog with each model's credit
  multiplier (`chat --list-models`, a local listing — no API call) and then the
  effort enum with the recommended level marked; the picked pair is stored in
  history and replays exactly. `<prefix> kiro-cli` gained `model=<id>` /
  `effort=<level>` (and bare catalog IDs), which override either half of a
  preset regardless of argument order.
- Validate native-Kiro effort against the runtime enum
  (`low|medium|high|xhigh|max`). The Kiro CLI silently accepts an unknown
  `--effort` and falls back to its default, so a typo used to downgrade a session
  invisibly; the launcher now fails before exec.
- Kiro preset labels now carry operational intent between the icon and the
  model, matching the native-Codex profile labels.

- Add a Kiro-only MCP overlay, `mcp.kiro.local.json`. Kiro preparation merges it
  after the shared `.mcp.json`, `.mcp.local.json`, and `mcp.local.json` inputs,
  while Claude and Codex preparation continue to ignore it, so a server intended
  for a single runtime no longer has to be suppressed in the others. Duplicate
  server names across all four inputs are still rejected before any generated
  Kiro state is written.

- Resolve `${VAR}` and `${VAR:-default}` in Kiro MCP header values during
  preparation. Kiro CLI sends header values verbatim, so an env-indirect
  `Authorization` header reached the service as literal placeholder text and any
  such HTTP MCP server failed to authenticate, while the same definition worked
  in Claude and Codex. A missing variable without a default resolves empty and
  is reported by name. Generated files that can now hold a resolved credential
  (`settings/mcp.json`, `agents/*.json`) are written owner-only.
  **Superseded below:** Kiro ignores `headers` on http transport entirely, so
  resolving them changed nothing and only put credentials on disk.

- Bridge header-authenticated HTTP MCP servers for Kiro instead of resolving
  their headers. The upstream agent schema takes only `url` into account for http
  transport, so Kiro dropped the headers and failed with
  `OAuth discovery failed: the server does not advertise OAuth endpoints`.
  Preparation now rewrites such a server into an equivalent pinned `mcp-remote` stdio
  bridge that carries the headers, passing `${VAR}` through unresolved so the
  bridge substitutes it from the inherited environment. No credential reaches
  argv or generated state, which also reverts the owner-only file modes that the
  previous approach required. HTTP servers without headers, servers already using
  stdio, and the SSH-tunnel light filter are unaffected.

- Add a profile-scoped default-isolation canary for fresh interactive
  direct-Claude and native-Codex sessions, with exact-UUID continuation,
  explicit `--no-isolated` rollback, and allocation-free rejection of
  ambiguous continuation commands.
- Add per-UUID close-on-exec runtime leases and conservative terminal-workspace
  garbage collection with a validated 24-hour default retention window.
- Add validated native Codex `--app asdk_app_*` one-shot opt-ins. They merge
  with the trusted per-harness default allowlist for one launch without
  persisting connector exposure into the next session.
- Add opt-in `--isolated` / `HARNESS_SESSION_ISOLATION=1` root sessions. Each
  launch receives a remote-free repository with an independent Git directory
  and an atomic, durable session journal;
  the default launcher path remains unchanged.
- Add a local serialized submission broker with digest-bound patches, manifests,
  canonical origin, non-force remote delivery, repository-owned candidate
  verification, remote CAS acknowledgement, exact delivered-commit readback,
  clean-session closeout, and crash reconciliation.
- Add `harness-session` lifecycle commands, UUID workspace resume, live-session
  heartbeats, and kernel-released integration locking for crash recovery.

### Changed

- Bump the native-Kiro session and subagent tiers to the current generation:
  sonnet → `claude-sonnet-5`, opus → `claude-opus-5`. The haiku tier stays on
  `claude-haiku-4.5` (no newer haiku in the catalog). The bump is rate-neutral:
  `chat --list-models` reports the same `rate_multiplier` and 1M context window
  for opus 4.6/4.7/4.8/5 (2.2x) and for sonnet 4.6/5 (1.3x).

### Fixed

- Route native Codex `Bash` PreToolUse payloads directly to the canonical PR
  and harness-main-only guards, while strictly parsing only exact
  `code_mode_exec`/`exec` composite inputs. Both routes validate the payload
  cwd, and generated strict guard matchers now cover all three identities
  without widening other Bash hooks.

## 0.29.4 — 2026-09-11

### Fixed

- Run the PR and harness-main-only Codex guards against each statically
  declared composite exec command and its real workdir. Dynamic arguments fail
  closed with an inline-literal retry instead of bypassing command policy;
  ambiguous JavaScript and malformed canonical-hook output also fail closed.

## 0.29.1 — 2026-09-09

### Changed

- Prefer the compiler's project-aware Codex AGENTS route, which avoids a second
  runtime-contract copy. Older harness compilers fall back to the unchanged
  legacy route and retain the complete contract.

## 0.29.0 — 2026-09-09

### Added

- Launcher-owned Claude cmux title synchronization. Exact SessionStart handoffs
  follow native automatic and custom titles, including later manual renames and
  session switches within the same process.

### Fixed

- Keep title ownership live during slow cmux requests, preserve externally
  changed tab names, and clean up direct-launch brokers when the runtime exits.
- Export the selected profile prefix to Claude title hooks.

## 0.28.2 — 2026-09-09

### Fixed

- Keep Codex preparation warm when a regular `.in_use` runtime marker changes.
  Directory, symlink, and entry-type changes still invalidate the fingerprint.

### Changed

- Run reviewed integration tests with isolated fixtures in at most two shards.
  Security and new tests remain serial; `HARNESS_SURFACE_TEST_JOBS=1` retains
  the original complete serial test command.

## 0.28.1 — 2026-09-09

### Fixed

- Explicitly enable Codex native task progress after the upstream tool became
  opt-in. Warm preparation repairs missing or disabled tracker configuration.

## 0.28.0 — 2026-09-08

### Added

- Opt-in external agent filename namespaces in Codex surface manifests.
  Provider-owned native agent files survive warm and cold preparation, and
  corresponding Claude definitions are not converted over their native peers.
  Unlisted files and symlinks retain the existing quarantine behavior.

## 0.27.0 — 2026-09-08

### Added

- Opt-in Claude direct `fable` preset with high effort, plus a Fable entry in
  the preset and Custom model menus and shell completion. Existing numbered
  menu choices remain stable; Kiro and Codex gateways reject the preset.

## 0.26.2 — 2026-09-08

### Fixed

- Codex tab-title synchronization retries transient cmux errors with bounded
  backoff and stops retrying when its owning process exits. Local diagnostics
  distinguish recovery from exhausted retries without storing title contents.

## 0.26.1 — 2026-09-08

### Fixed

- `HARNESS_CODEX_APPS_ALLOWLIST` now invalidates a converged Codex home when
  ids are added, replaced, or removed. The regenerated policy keeps only the
  configured apps and returns to the warm path afterward.

## 0.26.0 — 2026-09-07

### Added

- `HARNESS_CODEX_APPS_ALLOWLIST` opts a project into named ChatGPT Apps. The
  feature flag stays off without it, and `[apps._default]` keeps unnamed apps
  disabled when it is set.

## 0.25.0 — 2026-09-06

### Added

- Generated profile inspection separates managed settings, project trust, and
  model-picker UI metadata while retaining full output drift detection.

### Fixed

- Codex hook adaptation supplies explicit runtime identity to canonical hooks
  instead of relying only on the inherited Codex home.

## 0.24.0 — 2026-09-06

### Added

- Read-only generated Codex profile inspection with setting provenance, output
  stamp consistency, and explicit limits on runtime and provider verification.

### Fixed

- Canonicalize equivalent Homebrew compatibility and opt preparation entrypoints
  so hook command hashes remain stable when callers use either path.

## [Unreleased]

### Added

- Generated Codex hook homes now honor the harness-owned `codex_exclusions`
  policy for `Stop` instead of applying a launcher-side exclusion. This allows
  a harness to register its direct `session-end.sh` delivery gate deliberately.

### Fixed

- Keep the Codex home integration suite independent of neighboring harness
  repositories by using a launcher-owned legacy compiler fixture.

### Changed

- Keep launcher-generated Codex custom subagent roles at a 272,000-token
  context window with a 217,600-token auto-compact limit, even when the main
  session explicitly selects 1M context. Built-in and ad-hoc Codex agents are
  outside this generated-role configuration, as are preserved provider-owned
  external roles such as synchronized `glider-*` TOMLs.
- Split the Codex home integration suite into three isolated fixture groups and
  run them with bounded concurrency. Optional JSON reports include each group's
  wall time, and Codex surface reports now include individual-test wall times.
- Run 36 reviewed private-fixture Codex surface tests in two balanced shards
  while keeping signal, publication, auth, revocation, quarantine, and new tests
  serial.

- The Codex hook adapter now passes its parent PID to the wrapped canonical
  hook as `HARNESS_HOOK_OWNER_PID`, enabling a paired direct Stop hook to
  recognize the same Codex hook parent without changing advisory hook behavior.
- Native Codex context is selectable instead of always long. TUI and shortcut
  launches default to `272k`, emitting `model_context_window = 272000` with
  auto-compaction at 217600 (80%); the `🧠 Context` toggle or `codex ... 1m`
  keyword opts into `1000000` with the existing 414000 compact line. History
  replay and warm validation preserve the selected mode. This avoids the
  documented GPT-5.6 API long-context multiplier for routine API-key requests
  and reduces retained context for ChatGPT-plan sessions, whose exact quota
  multiplier is not published.

## [0.23.0] — 2026-09-06

### Added

- Explicit native Codex `astra` profile selects GPT-6 Astra with medium reasoning
  from shell shortcuts and the launcher menu. Existing profiles and defaults
  retain their models. Generated profiles participate in warm-cache validation
  and repair.

### Fixed

- Local HTTP test fixtures no longer depend on reverse DNS before signaling
  readiness; their HTTP and redirect assertions are unchanged.

## [0.22.4] — 2026-09-06

### Added

- Generated Codex homes now enable experimental context management when the
  resolved Codex CLI is 0.153.0 or newer. Eligible ChatGPT sessions can retain
  long-thread details through notes and searchable history instead of relying
  only on a repeatedly compressed summary. Older clients omit the nested table
  because Codex 0.152.1 rejects that shape during bootstrap; API-key and custom
  provider eligibility remains controlled upstream.

### Changed

- Context management leaves the existing long-context request and auto-compact
  guard unchanged at `model_context_window = 1000000` and
  `model_auto_compact_token_limit = 414000`.

## [0.22.3] — 2026-09-06

### Added

- Project-owned MCP surface policy values: empty preserves legacy selectors,
  `single-full-compat` accepts direct Claude `light` and Codex `work` selectors
  as full with a deprecation warning, and `single-full` rejects those retired
  selectors. The policy is read only from the trusted project `launcher.env`;
  Kiro keeps its native light/full selection in every mode.
- Opt-in launchpad histories migrate to the full surface before display through
  a validate-then-rewrite transaction. Migration canonicalizes entries, sorts
  and de-duplicates them, bounds retained history, and atomically replaces the
  history file only after the replacement is complete.
- Exact MCP profile policies can now emit positive startup/tool timeouts,
  `required`, default tool approval, and per-tool approval in addition to tool
  allow/deny lists. Validation fails closed on invalid types or approval modes,
  and the warm-home check includes every emitted policy field.
- New Claude Code `opus` preset: the `rich` model at one effort step down
  (`opus[1m]` at `high` on Anthropic direct, `claude-opus-4-6[1m]` at `high`
  through a Kiro gateway, `opus` plus the configured context suffix through a
  Codex gateway). `rich` stays the deepest preset at `xhigh` with forced
  thinking; `opus` keeps the user's own thinking setting because `high` needs
  no override. It appears in the TUI mode menu between `plan` and `rich`, and
  in shell completion.

### Changed

- Generated Codex homes include the native `task-progress` status-line item, so
  `update_plan` progress stays visible without a launcher-owned task store.
- Generated Codex homes now opt into long context. `config.toml` requests
  `model_context_window = 1000000`, which Codex clamps to the model's own
  `max_context_window` — 872000 for the GPT-5.6 luna/terra/sol models every
  mode profile and subagent tier uses, an effective 828400 at the reported 95%.
  Sessions previously resolved the 272000 default, an effective 258400.
  `model_auto_compact_token_limit = 414000` puts the auto-compact line at half
  that effective window, matching the Claude side (`autoCompactWindow` 1000000
  at `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` 50 compacts at 500000 on a real 1M
  window). The keys were unpinned in 0.8.4 because the values then in use
  (1050000 / 900000) scheduled compaction past the real backend ceiling, so it
  never fired; the limit now stays below the effective window by construction.
- Codex subagents mapped from the Claude `opus` tier now default to GPT-5.6 Sol
  with medium reasoning effort. High effort remains an explicit escalation for
  unusually risky or complex reviews instead of the baseline for every review.
- Native Codex launch entrypoints now normalize and isolate the optional
  `HARNESS_CODEX_GLOBAL_MCP_ALLOWLIST` from each trusted `launcher.env` before
  preparation, preventing configured, empty, or inherited values from leaking
  across launches.

## [0.20.3] — 2026-07-27

### Added

- Each harness now derives its own `GH_TOKEN` on launch. `gh` keeps a single
  global active account, but the harnesses legitimately expect different GitHub
  users (personal and team identities), so whichever harness
  switched last decided whether the others' `gh` commands succeeded. On entry
  `harness_gh_token_load` reads `github_user` from the harness' own
  `config/.local/config.yaml` (falling back to `config/config.yaml`) and exports
  the matching `gh auth token --user` value, which takes precedence over the
  stored credentials. Strictly fail-open: a missing config, an unknown user, a
  malformed `github_user`, or no `gh` on `PATH` exports nothing and leaves the
  `pre-bash-gh-auth` hook as the backstop — an empty `GH_TOKEN` would override
  the stored credentials with nothing. The token value is never printed.

## [0.20.2] — 2026-07-27

### Fixed

- `rich` and `ultracode` resolve to `xhigh` (and kiro `rich` to `max`), which the
  API rejects while thinking is disabled: `output_config.effort 'xhigh' is not
  supported when thinking is disabled on this model`. A machine with
  `alwaysThinkingEnabled: false` in `settings.json` therefore 400'd on the first
  prompt of every rich session. Those two efforts now launch with
  `--settings '{"alwaysThinkingEnabled":true}'` so the mode no longer depends on
  per-machine settings. `high` and below are untouched and keep the user's own
  choice; `--settings` merges, so unrelated settings survive.

## [0.20.1] — 2026-07-24

### Changed

- MCP profile resolution now treats `enabled` as a wish-list: a server enabled in
  a profile but absent from every definition source is dropped with a stderr note
  instead of failing the launcher. A gitignored `mcp.local.json` thereby decides
  per-host MCP exposure — a machine without access to a given server (e.g. a
  host-local RAG backend) starts cleanly instead of dropping to the menu.

## [0.20.0] — 2026-07-23

### Changed

- Generalized cmux title and metadata-only observability profile validation to
  every valid registered prefix instead of a private fixed alias set.

### Security

- Removed private profile aliases from tracked code, tests, and documentation,
  with a repository-wide public-sanitization regression gate.

## [0.19.4] — 2026-07-22

### Changed

- Disabled standalone source installation. `install.sh` now exits without writing and directs users to Homebrew.
- Moved runtime-copy setup used by integration tests into a test-only fixture helper.

### Security

- Eliminated the source installer's multi-file commit and rollback surface, including parent-swap TOCTOU writes and cleanup outside the intended prefix.

## [0.19.3] — 2026-07-22

### Fixed

- Reject every existing symlink component and any `..` traversal in the install prefix, share path, and bin path before staging and again immediately before commit.
- Cover symlinked prefixes, symlinked `share` parents, and symlinked prefix ancestors without writing through them.

## [0.19.2] — 2026-07-22

### Fixed

- Removed marker- and filename-based share ownership classification because either proof was forgeable.
- Made source installation additive and fail-closed: missing assets are staged and installed, byte-identical existing assets are left untouched, and differing existing assets abort before any write.
- Kept rollback only for newly added assets, eliminating overwrite and restore paths for pre-existing share files.

## [0.19.1] — 2026-07-22

### Fixed

- Made the standalone source installer transactional: all assets and entrypoints are staged before commit, and a late failure restores the prior managed installation.
- Refused foreign regular files, foreign symlinks, and share-path symlinks instead of replacing or following them.
- Added a managed-share ownership marker while retaining verified upgrades from pre-marker installations.
- Added a real `harness-auto` → `harness-exec` → Claude policy-path test for prompt, resume, and permission argument preservation.

## [0.19.0] — 2026-07-22

### Added

- Add `harness-auto`, a fail-closed external-agent adapter that selects the
  single most-specific registered harness from the canonical current directory
  and then delegates unchanged agent arguments to `harness-exec`. It rejects
  unmatched workspaces, symlink escapes, registry symlinks, and ambiguous
  profile registrations.

### Changed

- Orca may now use `harness-auto` as its Claude, Codex, and Kiro command
  override, so built-in agent startup preserves the selected profile policy
  without a manually selected profile command.

## [0.18.0] — 2026-07-21

### Added

- Add `harness-profile register`, which installs any trusted harness prefix as
  a real executable backed by a small profile registry instead of requiring
  interactive Zsh startup. Registration is fail-closed and preserves existing
  command and profile ownership across trust boundaries.

### Changed

- Registered prefix functions and executable profile commands now share the
  `harness-exec` contract. They automatically keep the current workspace when
  it resolves inside the harness, preserve the harness-root default elsewhere,
  and retain explicit `--cwd` precedence and symlink-escape rejection. Prefix
  registration also rejects unsafe function names and shell-quotes harness paths.

## [0.16.0] — 2026-07-21

### Added

- Add `harness-exec`, a real non-interactive executable for Orca and other
  workspace managers. It supports an explicit profile-local `--cwd`, rejects
  missing or out-of-bound directories, preserves project-scoped Codex/Kiro
  homes, and forwards the same working directory through direct and TUI
  Claude, Codex, and Kiro launches.

## [0.15.2] — 2026-07-16

### Fixed

- Keep the cmux title watcher under the live launcher/Codex process ancestry
  and use `SessionStart` only to hand off the exact session ID. This preserves
  cmux caller authorization after the short-lived hook process exits.

## [0.15.1] — 2026-07-16

### Fixed

- Allow healthy but slow cmux tab rename round trips to complete instead of
  stopping the Codex title watcher at the previous two-second timeout.

## [0.15.0] — 2026-07-16

### Changed

- **Codex puts actionable context first.** The native terminal title now uses
  `activity | thread-title | project-name`, while the footer places
  `context-remaining` before `branch-changes`.
- **cmux tabs use the short harness alias after the thread name.** Registered
  profile launches export their dynamically scoped prefix, and a fail-open
  SessionStart watcher labels only the exact starting tab as
  `<thread name> | <profile>`. The watcher reads the matching session ID only,
  sanitizes controls, suppresses duplicate writes, and stops with its Codex
  owner or an unavailable cmux surface.

### Added

- Packaged `codex-cmux-title-sync.py` beside the other launcher-owned runtime
  adapters and included it in generated-surface fingerprinting.

## [0.14.1] — 2026-07-15

### Changed

- **Codex profile intent is explicit in the launcher UI.** `base` is labeled
  `Everyday · Recommended`, `sol` is labeled `Stronger · slower`, and `rich`
  is labeled `Deep · slowest`, while each row continues to show the generated
  model and effort. The underlying profile routing, the default `base`
  selection, and saved-history replay are unchanged. Reviewer subagents may
  still route independently to Sol/high even when the main session uses base.

## [0.14.0] — 2026-07-15

### Added

- **Subagent model/effort routing is now a single source of truth.**
  `bin/subagent-model-map.tsv` maps each Claude subagent frontmatter tier
  (haiku/sonnet/opus) to a per-runtime model + effort, and both
  `codex-home-prepare.sh` and `kiro-home-prepare.sh` read it so the three
  runtimes cannot silently drift. The Codex tier mapping is empirically tuned
  (gpt-5.6 luna/terra/sol measured on representative subagent tasks), not a
  mechanical opus→flagship lift: `haiku→luna/low`, `sonnet→terra/medium`,
  `opus→sol/high`. Missing table falls back to the same literals.
- **Kiro CLI now gets per-subagent agents.** `.claude/agents/*.md` are converted
  to `$KIRO_HOME/agents/<name>.json` with the tier-resolved Kiro model ID
  (column 4 of the map) and read-only vs workspace `allowedTools` derived from
  the agent's declared tools, so `chat.enableDelegate` can route to each
  subagent on its intended model. Generated files carry an ownership marker and
  are reversible-quarantined on source removal; the launcher's own
  `harness.json` and any hand-authored agent JSON are never touched. Effort is
  intentionally left to the session default — the Q/Kiro agent schema has no
  per-agent effort field.

## [0.13.0] — 2026-07-15

### Changed

- **Launchpad ordering.** "New …" composer entries now sit at the top of the
  launchpad with recent configurations listed below them, and the gum filter
  viewport is tall enough to show every row (header/input no longer eat list
  lines).
- **Codex `work` is now an MCP-surface toggle, matching Claude/Kiro `light`.**
  The Profile menu lists model profiles only; `work` moved to a
  `🔌 MCP surface: default|work` toggle on the summary screen and combines
  with any profile (previously base-only). The shortcut gained the same
  freedom: `<prefix> codex rich work` / `<prefix> codex work sol` both select
  the profile and the work surface, and `work` stays a surface keyword
  regardless of order with session/safety keywords (`continue work`,
  `full-auto work`, …) — only a genuinely free-form token before it (e.g.
  `codex exec work`) demotes it to prompt text. Happy + work is rejected
  (toggle exclusivity in the TUI, fail-closed before home preparation in the
  shortcut).

## [0.12.0] — 2026-07-15

### Added

- **Light MCP surface for Claude Code and Kiro CLI.** `light` drops SSH-backed
  MCP servers — `core/bin/start-ssh-mcp.sh` stdio wrappers and loopback HTTP
  servers on the SSH-tunnel port band 38200–38299 — and keeps everything else,
  unifying the MCP-surface concept across all three runtimes (Codex keeps its
  `work` profile):
  - TUI: a `🔌 MCP surface: full|light` toggle on the Claude and Kiro summary
    screens.
  - Shortcuts: `<prefix> light` (Claude, via `--strict-mcp-config` + a
    generated `.harness/claude/mcp-light.json`) and `<prefix> kiro-cli light`
    (via `HARNESS_KIRO_MCP_PROFILE=light` at home preparation).
  - The generated light config revalidates duplicate server names on every
    launch, matching the full-surface validation.

### Changed

- **Launchpad TUI.** The top screen now lists your recent launch configurations
  (up to 8, newest first, per-harness `.harness/launcher-history`) plus one
  "New …" composer entry per installed runtime, fuzzy-searchable under gum
  (type to filter, Enter to launch). Picking a history row replays that exact
  configuration through the same assembly path as a fresh config. Entries are
  deduped by config identity — relaunching an old row moves it to the top
  instead of duplicating it. The previous single-entry "Repeat last"
  (`.harness/launcher-last`) is migrated into the history automatically.

## [0.11.0] — 2026-07-15

### Added

- Codex `sol` profile shortcut (`<prefix> codex sol`, TUI Profile menu):
  GPT-5.6 Sol at medium effort — the stronger model at everyday effort.

### Changed

- Label Codex Fast/Base/Plan/Rich routes as operational speed, balanced, or deep presets and document how they differ from general model starting effort; generated routing remains unchanged. (In the TUI these labels are now generated from the profile configs.)
- **TUI v2 redesign.** Choice collection and command assembly are now separate;
  every launch (including replays) revalidates gateways, generated homes, and
  MCP configs through one assembly path per runtime.
  - **Repeat last**: the first menu relaunches the previous configuration in one
    keypress (per-harness `.harness/launcher-last`).
  - Step diet: permission mode, Chrome, and Happy are toggles on a single
    launch-summary screen instead of chained yes/no prompts.
  - Esc/`q` is one-step-back everywhere (top menu exits); the no-gum fallback
    menu reprompts on invalid input instead of silently exiting.
  - Gateway health probes run in the background at startup (no 2s×N blocking)
    and the provider menu shows live 🟢/🔴/⚪ marks.
  - Breadcrumb headers (`harness ▸ runtime ▸ session`) and a unified launch
    banner across Claude/Codex/Kiro.
- **Single source of truth** (`bin/harness-common.sh`) shared by the TUI, the
  shortcut path, and tab completion: mode→model/effort tables (Claude + Kiro),
  binary resolution, gateway probes, MCP local-config validation, secrets
  export, and auto-compact PCT. Menu labels and completion descriptions are
  generated from the table, so they can no longer disagree with what launches.
  Native-Codex profile labels are read from the generated profile configs.
- TUI work MCP surface is now base-profile-only (a Profile-menu entry), matching
  the shortcut path; the old separate surface menu that allowed rich/plan + work
  combinations is removed.
- Shortcut parity: `codex fork` now forks the last session like the TUI, and
  `codex full-auto|never|bypass` map to the Codex safety flags instead of
  passing through as prompt text.

### Fixed

- TUI no longer launches when `.mcp.local.json`/`mcp.local.json` duplicates a
  committed `.mcp.json` MCP server (validation failure now blocks, matching the
  shortcut path).
- Fast preset label claimed Sonnet while launching Haiku; labels are now
  derived from the mode table (tab-completion text included).
- TUI honors `HARNESS_KIRO_BIN` for Kiro runtime detection and launch.
- Ultracode session hint no longer leaks into a different mode picked after
  backing out of the ultracode selection.
- `cd "$HARNESS_DIR"` failures abort the launcher instead of starting sessions
  in the wrong directory.
- Tab completion no longer leaks gateway API keys into interactive shell
  variables (env files are sourced in subshells).
- String→`xargs` argument round-trip removed; arguments are arrays end-to-end,
  so values containing quotes/spaces can no longer silently wipe the command
  line.
- Keep the manifest warm-path validator aligned with the Codex 0.144 status-line fields so unchanged homes no longer rebuild on every launch.

## [0.10.2] — 2026-07-11

### Fixed

- Track curated-plugin skill directory membership separately from volatile install metadata, preventing metadata-only rewrites from forcing a cold surface rebuild while still detecting new plugin versions.

## [0.10.1] — 2026-07-11

### Fixed

- Pin the Homebrew runtime to Python 3.13 and prefer its versioned path, avoiding a Python 3.14 `pyexpat` bottle incompatibility observed on a supported macOS/Xcode combination.

## [0.10.0] — 2026-07-11

### Added

- Schema-v1 `config/codex-surface.json` resolution for exact skill, Claude-plugin, Codex-only, and MCP profile membership.
- Atomic skill catalogs and successful-input fingerprints for validated warm Codex-home preparation.
- Manifest-governed explicit command wrappers that cannot overwrite a canonical project skill with the same name.
- Optional namespaces for package-scoped Codex-only skill profiles.

### Changed

- Manifest-enabled homes collapse duplicate skill routes, keep explicit-only skills out of implicit prompt matching, and disable unselected routes by exact `SKILL.md` path.
- Manifest MCP profiles render explicit enabled flags and gate bundled Computer Use; warm no-op preparation now avoids compiler and plugin work.
- Warm validation uses cached source identities and plugin topology, preserving invalidation for newly installed unapproved plugins without hashing plugin tests, docs, or assets.
- Exact homes discard stale overrides of selected skills, quarantine unmanaged generated-home routes, catalog enabled product-plugin skills, and shell-quote hook paths safely.
- Warm stamps validate launcher-owned output hashes and a normalized managed-config projection, including complete MCP payloads, while preserving intended runtime trust and external-plugin state.
- Generated skill and agent inventories are exact; unexpected entries are moved intact to a reversible quarantine.
- Python 3.11 or newer is now required, with Homebrew/non-login path selection and an explicit `HARNESS_PYTHON_BIN` override.

## [0.9.5] — 2026-07-10

### Added

- Contributor guide, security policy, issue forms, pull request template, and CI workflow.
- Architecture, maintained Codex integration, and troubleshooting documentation.
- Generic project registration examples and public trust-boundary guidance.

### Fixed

- Source installation now includes the Kiro runtime-home adapter.
- Kiro MCP inputs are validated in external staging; duplicate server names leave no new or modified generated runtime state.
- Codex takes the global cache lock only when bundled marketplace or plugin work needs synchronization.

### Changed

- Reorganized the README for public installation, onboarding, development, and support.
- Replaced the stale native Codex implementation plan with maintained integration documentation.

## [0.9.4] — 2026-07-10

### Fixed

- Added compatibility for retained macOS Chrome plugin caches whose native host is named `ChatGPT for Chrome`.
- Preserved the current `Codex for Chrome` name as the preferred host.

## [0.9.3] — 2026-07-10

### Added

- GPT-5.6 Codex profiles: Luna/low for fast, Terra/medium for base and default, and Sol/high for plan and rich.
- Project-local `.codex-only/skills` discovery.
- Chrome bridge and Computer Use plugin preparation for terminal Codex.
- Generated Codex agent model mapping by capability tier.

### Changed

- Codex context and compaction values now follow runtime model metadata instead of launcher pins.
- Terminal Codex from `PATH` is preferred over the app-bundled CLI.
- Shared global plugin/cache writes use macOS kernel locking.

### Security

- Browser execution trust uses exact browser-client SHA allowlisting.
- Project-writable runtime homes are not added as broad trusted code paths.
- Global MCP drift warnings are opt-in.

## 0.9.2 — 2026-07-09

### Fixed

- Made Codex CLI resolution deterministic across direct and interactive launcher paths.

[Unreleased]: https://github.com/kilhyeonjun/harness-launcher/compare/v0.22.3...HEAD
[0.22.3]: https://github.com/kilhyeonjun/harness-launcher/compare/v0.22.2...v0.22.3
[0.16.0]: https://github.com/kilhyeonjun/harness-launcher/compare/v0.15.2...v0.16.0
[0.15.2]: https://github.com/kilhyeonjun/harness-launcher/compare/v0.15.1...v0.15.2
[0.15.1]: https://github.com/kilhyeonjun/harness-launcher/compare/v0.15.0...v0.15.1
[0.15.0]: https://github.com/kilhyeonjun/harness-launcher/compare/v0.14.1...v0.15.0
[0.14.1]: https://github.com/kilhyeonjun/harness-launcher/compare/v0.14.0...v0.14.1
[0.14.0]: https://github.com/kilhyeonjun/harness-launcher/compare/v0.13.0...v0.14.0
[0.13.0]: https://github.com/kilhyeonjun/harness-launcher/compare/v0.12.0...v0.13.0
[0.12.0]: https://github.com/kilhyeonjun/harness-launcher/compare/v0.11.0...v0.12.0
[0.11.0]: https://github.com/kilhyeonjun/harness-launcher/compare/v0.10.2...v0.11.0
[0.10.2]: https://github.com/kilhyeonjun/harness-launcher/compare/v0.10.1...v0.10.2
[0.10.1]: https://github.com/kilhyeonjun/harness-launcher/compare/v0.10.0...v0.10.1
[0.10.0]: https://github.com/kilhyeonjun/harness-launcher/compare/v0.9.5...v0.10.0
[0.9.5]: https://github.com/kilhyeonjun/harness-launcher/compare/v0.9.4...v0.9.5
[0.9.4]: https://github.com/kilhyeonjun/harness-launcher/compare/v0.9.3...v0.9.4
[0.9.3]: https://github.com/kilhyeonjun/harness-launcher/compare/v0.9.2...v0.9.3
