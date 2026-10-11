# herdr web ui

## Managed workspace creation and previous conversations

Codex command networking is a separate setting from approval and filesystem access. On supported releases, `options.network` accepts `enabled` or `disabled` together with an explicit `workspace-write` sandbox; the planner renders `-s workspace-write -c sandbox_workspace_write.network_access=true|false`. `danger-full-access` removes the filesystem sandbox and permits networking, so a conflicting disabled-network choice is refused. `never` only changes approval prompts.

Omitted settings retain Native defaults. Their effective sandbox and network values are `unknown` in the preview until independently verified; the planner does not silently choose a sandbox. Only an exact launcher-visible boolean becomes a network grant. The owned record and private archive preserve it; conversation text never grants permission and nested launches can only reduce it. A same-owner resume uses its immutable bound release, which must advertise network support before the Web adapter permits a new network selection. Older owners may keep existing settings or explicitly fork onto a supported release; a fork does not automatically copy dirty workspace files.

Hosts with a managed launcher release can expose registered profile capabilities to the Web UI. Quick start preserves the profile preset. Detailed controls use the same pure `harness_launch_plan.py` planner as the terminal JSON interface, validate model/effort/context combinations, and preview the exact launcher arguments before an explicit Start. Codex approval and sandbox remain separate controls; existing source policy, including Slack approval requirements, is preserved. Claude offers standard or 1m native context aliases. Requested context is configuration, not a guarantee of model or account support.

Previous conversations are selected by a target-local opaque catalog ID, never `--last`. Exact resume retains its launcher owner, home and bound release. A retired conversation is copied from a verified private archive into a fresh owner for an explicit fork; the original journal/archive remain unchanged. Missing permission provenance requires an explicit permission selection. Raw conversation text and authentication are not returned by the catalog or copied to another PC/profile.

Favorites and recent selections restore validated settings on the selected PC. Picking one never starts an agent. Recent entries are recorded only after the owned process is confirmed started. Uncertain startup reconciles the existing request and workspace without resending terminal input.

Isolation GC archives supported Codex native data before retiring a terminal worktree. `HARNESS_SESSION_RETENTION_SECONDS=archive` enables the compatible default policy: old numeric-only engines refuse cleanup before deletion. Managed hosts must also route ordinary `harness-headless` through the verified selected release because old headless drops this environment variable. Explicit execution of an old absolute headless binary is outside that managed route.

[herdr web ui](https://github.com/devswha/herdr-web-ui) is a herdr plugin: a browser client with chat, a live terminal, approval cards, and a file browser for every agent pane. `harness-herdr-web` installs it **local-only, behind a token**, and checks that it stays that way.

Supported: the pinned release `v0.3.34` (commit `efd017b1a42c`) on macOS, with herdr 0.9.1 and Bun 1.4 or later.

## Why a wrapper

Agent panes on a harness host usually run with automatic approval, so anything that can type into a pane can run commands as you. Upstream serves local callers without authentication when `HERDR_WEB_TOKEN` is unset. The wrapper therefore always sets a token of at least 32 characters, which upstream then requires from local callers too.

**A machine where Tailscale reports a login is not supported** until a later reviewed release. `check` and `install` fail there, whether Tailscale is running or stopped.

The token does not protect against local agents or processes running as you: they can read the env file.

## Commands

```bash
harness-herdr-web install    # gate, configure, install the pinned release, start it, then check
harness-herdr-web check      # OK/WARN/FAIL report; exit 1 on FAIL
harness-herdr-web configure  # enforce the managed keys and file modes
harness-herdr-web token --copy   # put the token on the clipboard for the first browser login
harness-herdr-web usage      # one line of Claude and Codex plan usage, for herdr's tab bar
harness-herdr-web usage --watch  # the same usage as a live board, for a herdr popup
```

Open `http://127.0.0.1:7317` (or your `PORT`) and paste the token when asked. The session cookie is scoped to `127.0.0.1`, so every local port receives it. `pbcopy` also syncs through Universal Clipboard.

`usage` reads the plugin's `/api/usage` with the token (the plugin caches it for up to five minutes) and prints, for example, `🔴 Claude 5h 14% 0.2× ↻12:10 · 7d 60% 1.9× →금21시 ↻화07시 │ 🟢 Codex 7d 48% 0.8× ↻일03시`. Each window shows the used share, its burn pace against an even spend (`1.0×` uses the window up exactly at its reset), `→` the expected exhaustion time when the current rate runs out before the reset, and `↻` the reset. The rate is the slope over the last three hours of readings, kept in `~/.local/state/harness-launcher/herdr-usage.json` (`$XDG_STATE_HOME` when set), or the window average until such a reading exists; the 5-hour window shows no pace in its first hour. herdr strips escape sequences from status commands, so the provider badge is an emoji: 🔴 runs out before the reset or is used up, 🟡 ends the window at 85% or more or is at 80% now, 🟢 otherwise. It never prints the token, and prints `usage n/a` when the web ui is unreachable. Show it in herdr's tab bar:

```toml
# ~/.config/herdr/config.toml
[ui]
tab_bar_right = [
  { type = "command", command = "/opt/homebrew/bin/harness-herdr-web usage", interval_seconds = 60, timeout_seconds = 15 },
  { type = "datetime", format = "%H:%M" },
]
```

`usage --watch` draws the details as a board: per window a bar of the used share with `│` at the share of the window elapsed (fill past the tick runs ahead of an even spend), the used share, the pace, and the reset time with the time left. A window that runs out before its reset gets a red line with the expected exhaustion time; one heading for 85% or more gets a yellow line with the projected share. It reads again every 60 seconds or on `r`, keeps the last board with a warning when a reading fails, and closes on `q`, `Esc` or `Ctrl-C`; under the Korean input method `ㄱ` and `ㅂ` act as `r` and `q`. Text from the server is printed without control characters. Without a terminal it prints the board once. Open it with a key:

```toml
# ~/.config/herdr/config.toml
[[keys.command]]
key = "prefix+u"
type = "popup"
command = "/opt/homebrew/bin/harness-herdr-web usage --watch"
width = "80%"
height = "60%"
```

Both read and write the same sample store. Plan usage is per account, so the [herdr plugin](terminal-runtimes.md#herdr-plugin) does not report it per pane.

`install` refuses before installing anything in two cases: Tailscale reports a login for this machine, or `bun --version` is below 1.4 (fix with `bun upgrade`). herdr runs plugin commands with its own `PATH`, so `bun` must be reachable from the herdr server as well. If the build fails, check `herdr plugin log devswha.herdr-web-ui`. `install --ref <tag>` installs another ref and warns that the ref is unreviewed.

## Managed configuration

The plugin reads `$(herdr plugin config-dir devswha.herdr-web-ui)/env` and then `.env`; values in `.env` win. `configure` writes only `env`:

| Key | Value |
| --- | --- |
| `HERDR_WEB_TOKEN` | Kept if it has at least 32 characters, otherwise replaced with 64 hex characters (never printed) |
| `HOST` | `127.0.0.1` |
| `HERDR_WEB_AUTO_UPDATE` | `0`. Upstream only auto-installs on `1`. |

`configure` also has these rules:

- It keeps other lines as they are, and replaces duplicate managed keys with one line.
- It sets the directory to `0700` and the files to `0600`.
- It refuses to write when:
  - a path is a symlink or is not owned by you;
  - `env` has an `export <managed key>` line (upstream reads `export HOST` as a different key);
  - `.env` sets a managed key.

After `configure` changes a running server's settings, restart the plugin:

```bash
herdr plugin action invoke devswha.herdr-web-ui.stop
herdr plugin action invoke devswha.herdr-web-ui.start
```

## What `check` verifies

- The effective token (from `env`, then `.env`) exists and has at least 32 characters.
- The effective `HOST` is `127.0.0.1`, `HERDR_WEB_AUTO_UPDATE` is not `1`, and the directory and files have their modes and are not symlinks.
- Tailscale does not report a login for this machine. The CLI is looked up on `PATH` or in `/Applications/Tailscale.app`. Whether the backend is running or stopped makes no difference.
- The installed checkout is the pinned commit. A different commit gives a WARN.
- `GET /api/health?scope=bridge` on the running server, sent without credentials, returns `auth.authenticated: false`. A server that accepts it, a missing `auth`, or a timeout gives a FAIL. A refused connection gives a WARN (not running).
- An in-app update that is running a different revision gives a WARN. The in-app updater still offers new releases in Settings; installing one there leaves the reviewed build.

## Do not

- Run upstream's `install.sh` or `bun scripts/plugin.ts phone`. Both run `tailscale serve --bg --yes`.
- Expose the port with `tailscale serve`, Funnel, a LAN bind, or a reverse proxy.
- Set `HERDR_WEB_AUTO_UPDATE=1`.

To remove the plugin:

```bash
herdr plugin action invoke devswha.herdr-web-ui.stop
herdr plugin uninstall devswha.herdr-web-ui
```
