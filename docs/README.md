# Documentation

The README covers installation and the first registered project. These documents explain the parts that matter when extending or debugging the launcher.

## User guides

- [Durable session integration](session-integration-queue.md) — explicit queue cutover, bounded execution, crash recovery, and service lifecycle.

- [Session disk diagnostics](session-report.md) — explicit read-only retention reasons, runtime metadata, and bounded size observations.

- [Trusted preservation](trusted-preservation.md) — one-shot, native-confirmed Git preservation.
- [Architecture and trust boundaries](architecture.md) — registration, command routing, generated runtime homes, and security boundaries.
- [Codex integration](codex-integration.md) — profile overlays, MCP translation, skills, plugins, hooks, and compatibility behavior.
- [Session titles in cmux](session-titles.md) — exact-session Claude/Codex title transport, native rename priority, and legacy hook coordination.
- [Codex surface manifests](codex-surface.md) — exact skill/plugin/MCP membership, host-token resolution, and warm launches.
- [Terminal runtimes](terminal-runtimes.md) — herdr/Orca/cmux/plain detection, environment scrubbing, `OSC 7`, the Codex status-hook registry, resume routing, and host prerequisites.
- [Orca ADE integration](orca-integration.md) — executable entrypoint, profile-local worktrees, agent status hooks, `CODEX_HOME` sanitization, resume mapping, ownership boundaries, and safety gates.
- [Paseo integration](paseo-integration.md) — `--passthrough` provider command, local workspace policy, daemon environment hygiene, and known limits.
- [herdr web ui](herdr-web-ui.md) — `harness-herdr-web`: local-only, token-gated browser client for herdr panes, and why Tailscale is blocked.
- [Troubleshooting](troubleshooting.md) — shell resolution, stale runtime binaries, generated state, MCP conflicts, and browser-host issues.

## Project policies

- [Contributing](../CONTRIBUTING.md)
- [Security policy](../SECURITY.md)
- [Code of conduct](../CODE_OF_CONDUCT.md)
- [Changelog](../CHANGELOG.md)
- [License](../LICENSE)

## Source map

| Path | Responsibility |
| --- | --- |
| `bin/aliases.zsh` | Project registration, shortcuts, completion, runtime wrappers |
| `bin/harness-auto` | Current-workspace profile resolver for external agent launchers |
| `bin/harness-exec` | Non-interactive executable entrypoint and canonical cwd resolver for external orchestrators |
| `bin/harness-profile` | Durable profile registry and executable prefix installer |
| `bin/harness_integration_queue.py` | Durable broker jobs, runtime/input custody, execution ownership and optional service |
| `bin/launcher.sh` | Interactive runtime/session/mode/safety picker |
| `bin/codex-home-prepare.sh` | Generated per-project Codex home |
| `bin/kiro-home-prepare.sh` | Generated per-project Kiro home |
| `bin/codex-hook-adapter.sh` | Supported hook translation into harness hooks |
| `bin/codex-pretool-adapter.py` | Strict raw-Bash/composite-exec normalization for command-sensitive Codex guards |
| `test/` | Isolated shell regression tests |
| `templates/project.gitignore` | Copyable generated-state and local-config ignore entries |
| `install.sh` | Disabled source-install migration stub; use Homebrew |

Generated files under a registered project's `.harness/` directory are runtime output, not documentation or configuration sources.
