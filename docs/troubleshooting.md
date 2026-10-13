# Troubleshooting

Run diagnostics from the same Zsh login environment that loads `aliases.zsh`. A command that works in a non-login shell can resolve a different Homebrew prefix, version-manager shim, or runtime binary.

## The project prefix is not a function

Check registration:

```zsh
zsh -lic 'whence -w wh'
```

Expected output:

```text
wh: function
```

If it is missing:

1. confirm `~/.zshrc` sources `aliases.zsh`;
2. confirm `harness_register` points to the right directory;
3. confirm `<project>/config/launcher.env` exists;
4. confirm `HARNESS_PREFIX` and `HARNESS_NAME` are set.

Reload with `exec zsh` after editing shell configuration.

## An alias shadows the prefix

The registration function removes an existing alias with the same name before defining the project function. Verify the final resolution:

```zsh
whence -w wh
```

If a plugin recreates its alias after registration, move the `harness_register` call below that plugin's initialization in `~/.zshrc`.

## `harness-exec` is missing or rejects the worktree

Check the installed external entrypoint:

```zsh
command -v harness-exec
harness-exec "/path/to/harness" --cwd "/path/to/harness/.worktrees/example" codex --version
```

Homebrew installations should be upgraded or reinstalled with `brew upgrade harness-launcher` or `brew reinstall harness-launcher`. Standalone source installation is disabled. A rejected `--cwd` must be moved under the owning harness; do not bypass the boundary with a symlink because canonical symlink targets outside the harness are rejected.

## The profile prefix works in Zsh but not in Orca or automation

Install the prefix as a real executable and ensure `~/.local/bin` is on `PATH`:

```zsh
harness-profile register "/path/to/harness"
command -v <prefix>
```

The executable reads only the canonical harness path from `~/.config/harness-launcher/profiles/<prefix>` and delegates to `harness-exec`. Registration is idempotent for the same canonical harness and launcher-owned command, but fails closed on regular files, foreign symlinks, registry symlinks, or a prefix already owned by another harness.

For Orca command overrides, also verify the path-aware adapter:

```zsh
command -v harness-auto
cd /path/inside/a/registered/harness
harness-auto codex --version
```

`no registered harness contains the current directory` means the Orca worktree is outside every registered harness boundary. Move only the disposable worktree base into the owning harness; do not move the harness or dirty source repository. `ambiguous registered harness boundary` means duplicate registry entries resolve to the same most-specific path and must be corrected before launch.

## Plain `codex` or `claude` does not select a harness

Plain-command routing is opt-in and depends on persistent profile entries:

```zsh
harness-profile register "/path/to/harness"
source "$(brew --prefix harness-launcher)/share/harness-launcher/aliases.zsh"
harness_shell_enable
```

Place `harness_shell_enable` after the launcher source line in `.zshrc`, then
run `exec zsh` after upgrading Homebrew. The enable command refuses to replace
an existing `claude` alias/function or a `codex` function no longer owned by
the launcher. Remove or rename that collision deliberately; the launcher will
not do so automatically.

An enabled command outside every registered boundary fails rather than falling
back to an unscoped runtime. Use `command codex` or `command claude` only when
that native, unharnessed launch is intentional. `harness_shell_disable` turns
off routing for the current shell.

If `claude mcp list`, `claude auth status`, `claude plugin list`, or
`claude doctor` reports an unexpected launcher option such as `--effort`, the
installed launcher predates the native-management route. Upgrade it, start a
new login shell, and retry with the management subcommand as the first token:

```zsh
brew update
brew upgrade harness-launcher
exec zsh
claude mcp list
```

These commands use the project selected from the current directory and load
its local environment, while Claude login state remains user-global. Native
`mcp list` reflects Claude's user/project discovery; it does not include a
launcher-only session overlay. Use `command claude --debug mcp list` when a
global option must precede the subcommand.

## `codex` reports `ENOENT`

A version-manager shim can exist while its platform package is missing. Check the executable selected by the login shell:

```zsh
zsh -lic 'command -v codex; codex --version'
```

If this resolves into a mise-managed Node installation and fails to spawn its native Codex binary, reinstall through that same environment:

```zsh
zsh -lic 'npm install -g @openai/codex@latest && mise reshim && codex --version'
```

Updating `/opt/homebrew/bin/codex` does not repair a different Codex selected earlier in `PATH`.

## Codex.app is installed but not selected

The terminal `codex` from `PATH` is preferred. This is intentional because Codex.app can bundle an older CLI.

Check both versions before opting into the app fallback:

```zsh
zsh -lic 'codex --version'
/Applications/Codex.app/Contents/Resources/codex --version
```

Enable fallback only when no terminal Codex is available:

```zsh
export HARNESS_CODEX_ALLOW_APP_FALLBACK=1
```

Use `HARNESS_CODEX_BIN=/absolute/path/to/codex` for a deliberate explicit binary.

## The wrong project receives `CODEX_HOME`

Direct `codex` wrapping only activates when `--cd` or `-C` points at a registered project, or at a directory containing `config/launcher.env`.

Inspect the target:

```zsh
codex --cd "/path/to/project" --version
```

Disable automatic preparation temporarily:

```zsh
HARNESS_LAUNCHER_DISABLE_CODEX_WRAPPER=1 codex --version
```

## HTTP MCP server fails only in Kiro

An HTTP MCP server that works in Claude and Codex but fails in Kiro with
`OAuth discovery failed: the server does not advertise OAuth endpoints` is
header-authenticated. Kiro's agent schema takes only `url` into account for http
transport, so the headers are dropped and the client tries OAuth instead.

Preparation now rewrites such a server into an `mcp-remote` stdio bridge. If one
still fails, check in this order:

```bash
# 1. Did preparation bridge it? Expect command "npx" and a --header argument.
python3 -c "import json;print(json.load(open('.harness/kiro/settings/mcp.json'))['mcpServers']['<name>'])"

# 2. Is the credential variable in the session environment the bridge inherits?
printenv MY_TOKEN >/dev/null && echo set || echo missing

# 3. Does the bridge reach the endpoint by hand?
npx -y mcp-remote@latest <url> --header "Authorization: Bearer \${MY_TOKEN}"
```

Keep the `${VAR}` placeholder in the committed header; `mcp-remote` substitutes it
from the environment. Deliver the value through `.claude/settings.local.json` env
or the harness's local environment, never in the committed `.mcp.json`.

## Duplicate MCP server error

Committed and local MCP files are merged. The same server name cannot appear in more than one file:

```text
.mcp.json
.mcp.local.json
mcp.local.json
mcp.kiro.local.json
```

Rename the local server or remove the duplicate. Local files extend committed config; they do not override it. `mcp.kiro.local.json` is Kiro-only but is not exempt from this rule. Native Kiro validates this before creating a fresh `.harness/kiro` home or changing an existing generated Kiro configuration.

Do not solve this by moving credentials into `.mcp.json`.

If the duplicate names a selected global Codex MCP, inspect both the project's configured allowlist and the exact surface source list:

```zsh
grep HARNESS_CODEX_GLOBAL_MCP_ALLOWLIST /path/to/project/config/launcher.env
grep -n codex-global-allowlist /path/to/project/config/codex-surface.json
```

The definition itself belongs in `$HOME/.codex/config.toml`; remove or rename the conflicting project/local/product definition. Do not use static `env` or `http_headers` to work around the validation error—use `env_vars`, `env_http_headers`, or `bearer_token_env_var` references instead.

## Generated Codex config looks stale

Do not edit `.harness/codex/*.toml` directly. Trigger preparation again:

```zsh
wh codex --help >/dev/null
```

For an exact surface, selected global MCP allowlist membership and the selected definitions in `$HOME/.codex/config.toml` are warm-path inputs. A change to either triggers regeneration automatically; unrelated global definitions do not. If diagnostics report a global MCP name as missing or invalid, correct `config/launcher.env` and the authoritative global TOML, then relaunch. An empty or omitted allowlist intentionally removes that projection on the next native launch.

If you need a clean disposable runtime home, first make sure no Codex session is using it, then move the generated directory aside:

```bash
mv .harness/codex ".harness/codex.backup.$(date +%s)"
```

Run the launcher again and compare the regenerated result. Keep the backup until sessions and auth links are verified.

For a project with `config/codex-surface.json`, inspect `.harness/codex/skill-catalog.json` to see the selected source path, hash, invocation policy, and MCP profile. A divergent duplicate without `duplicate_choices`, a missing profile-only skill, or an undeclared MCP server is a source error; editing generated TOML does not fix it.

To force one safe rebuild without deleting sessions, remove only the success stamp:

```bash
rm .harness/codex/.surface-success.json
```

## Lock file remains under `~/.codex`

This file is expected:

```text
~/.codex/.codex-home-prepare-global.lock
```

The file's presence does not mean the lock is held. Test nonblocking acquisition:

```bash
/usr/bin/lockf -s -k -t 0 "$HOME/.codex/.codex-home-prepare-global.lock" true
echo $?
```

Exit status `0` means the lock is free. Do not delete it as a routine cleanup step.

## Codex start waits for another session

```text
Waiting up to 300s for another Codex session to finish home preparation...
```

Preparation of one generated Codex home runs one session at a time. Sessions
that start together, such as a terminal multiplexer restoring its panes after a
reboot, queue on `.harness/codex/.codex-home-prepare.lock` and start in turn.
The notice appears after 5 seconds of waiting; no action is needed.

A session gives up with `timed out waiting for Codex home preparation lock`
after 300 seconds. Start it again, or raise the limit for a longer queue:

```bash
export HARNESS_CODEX_HOME_LOCK_TIMEOUT=900
```

The value is whole seconds, capped at 3600; zero or a non-number keeps 300.
Export it in the environment that starts the sessions (the shell profile, or
the environment the multiplexer server runs in). A line in
`config/launcher.env` is not passed on to preparation.

The limit applies to each lock on its own: the per-home lock first, then the
global cache lock under `~/.codex`, which one start takes once or twice.

Callers that must not block, such as a hook that refreshes the home in the
background, can export a small value to fail fast as before.

## Chrome bridge is not configured

Trigger preparation, then inspect available host configs:

```bash
python3 - <<'PY'
from pathlib import Path
root = Path.home() / ".codex/plugins/cache/openai-bundled/chrome"
for path in root.glob("*/extension-host/macos/*/extension-host-config.json"):
    print(path)
PY
```

Supported host executable names include current `Codex for Chrome` and legacy `ChatGPT for Chrome`. If neither the Codex app marketplace nor a valid global plugin cache exists, the launcher has nothing to materialize.

Do not manually add all of `~/.codex` or a project `CODEX_HOME` to trusted code paths. Browser execution trust must remain hash-based.

## Gateway health check fails

Gateway modes require a local env file and a reachable `/health` endpoint:

```text
config/.local/kiro-gateway.env
config/.local/codex-gateway.env
```

Check that the gateway is running and the configured URL is correct. Redact the URL and all key values before sharing logs.

## A project-scoped Claude plugin "isn't installed" in an isolated session

`claude plugin list` in an isolated session root reports:

```text
Plugin "<name>" is enabled in project settings but isn't installed
```

Claude Code applies a project- or local-scope install record only to the path it
was installed for, and the session root is a separate clone. Launchers from this
release copy the canonical root's records into the session root before Claude
Code starts. Start a new isolated session or continue the existing one with
`<prefix> --isolated-session <uuid> resume`; the next launch mirrors the record.
Do not run `claude plugin install --scope project` inside the session: the record
it adds is tied to that UUID path, and `--scope local` would edit the canonical
`.claude/settings.local.json` through its symlink.

If the plugin still fails to load, look for a `harness-launcher: warning:` line
at launch that mentions Claude plugin install records:

- `... not mirrored: <reason>`: the registry was malformed, not format version
  2, a symlink or hard link, locked by another launcher for 10 seconds, or kept
  changing; it was not rewritten. "changed right after the write" means the
  record was written but another process replaced the file at once; the next
  launch retries.
- `... were not mirrored into the isolated session`: the helper could not run
  (no Python 3.11+, or the local environment export failed).

Headless runs and launches with `HARNESS_CLAUDE_PLUGIN_MIRROR=0` in the launching
environment never mirror.

## Homebrew still shows an older launcher

```bash
brew update
brew info harness-launcher
brew upgrade harness-launcher
```

Confirm the linked installation:

```bash
brew list --versions harness-launcher
```

If you previously edited `/opt/homebrew/share/harness-launcher` directly, Homebrew can replace those changes. Durable fixes belong in the canonical repository and a tagged release.

## Reporting a bug

Use the bug report form and include:

```text
harness-launcher version
macOS version
zsh --version
selected runtime and version
registered command shape
redacted error output
```

Never attach auth files, `.claude/settings.local.json`, gateway env files, or unredacted MCP configuration. Security issues belong in the private reporting flow described in [SECURITY.md](../SECURITY.md).
