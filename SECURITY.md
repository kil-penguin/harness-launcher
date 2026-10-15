# Security policy

## Supported versions

Security fixes are released on the latest published version. Upgrade before reporting a bug that may already be fixed:

```bash
brew update
brew upgrade harness-launcher
```

| Version | Security support |
| --- | --- |
| Latest release | Supported |
| Older releases | Not supported |

## Reporting a vulnerability

Do not open a public issue for a vulnerability, credential leak, auth-boundary problem, or command-injection finding.

Use [GitHub's private vulnerability reporting form](https://github.com/kilhyeonjun/harness-launcher/security/advisories/new).

Include:

- affected version or commit;
- affected runtime and command shape;
- macOS and shell versions;
- impact and trust boundary crossed;
- minimal reproduction steps;
- a proposed fix, if you have one.

Remove tokens, API keys, auth files, cookies, account identifiers, internal URLs, and personal paths. A reproduction should use fake values and a disposable project directory.

The maintainer will acknowledge the report through the advisory, validate the impact, and coordinate a fix and disclosure. Please do not publish the issue before a patched release is available.

## Security model

`harness-launcher` executes local AI coding CLIs inside directories chosen by the user. It does not sandbox those CLIs beyond the selected runtime's own sandbox and approval settings.

Important boundaries:

- `config/launcher.env` is sourced as shell code. Register only trusted project directories.
- Runtime-specific state is generated under `<project>/.harness/<runtime>`. One exception: before an interactive isolated Claude session starts, the launcher copies the canonical root's project/local plugin install records into Claude Code's user registry (`installed_plugins.json`) for the session root and removes records for retired session roots. It never adds a plugin or install path the user did not already install, and never rewrites a malformed, symlinked or hard-linked registry.
- Native Codex auth remains in the user's Codex auth store; project homes reference it rather than copying tokens.
- `.claude/settings.local.json` can provide local environment values to child runtimes. Treat it as sensitive and keep it out of version control.
- `.mcp.local.json`, `mcp.local.json`, and `config/.local/**` are intended for machine-local configuration and secrets.
- Shared Codex plugin/cache writes are serialized with the macOS kernel lock.
- Opt-in harness sessions use detached per-UUID repositories with distinct Git directories and no remote plus a kernel-locked integration broker; only canonical-origin, non-force updates that pass the repository-owned verifier and whose delivered commit and exact path/mode/blob manifest are proven in fresh-cloned remote history become delivered.
- `node_repl` browser integration trusts exact browser-client hashes. Project-writable runtime homes are not trusted code paths.
- A gateway mode sends prompts and tool traffic to the configured gateway. Users are responsible for trusting that endpoint.
- The launcher prefers runtime binaries from `PATH`. `HARNESS_CODEX_BIN` and app fallback options deliberately change the executable trust decision.
- Workspace-aware profile commands use the current directory only when its canonical path remains inside the registered project. Explicit `harness-exec --cwd` follows the same resolver; symlink escapes and outside directories are rejected.
- `harness-auto` reads regular, non-symlink profile entries only. It selects the single longest canonical boundary containing the current directory and fails closed for unmatched or equally specific duplicate registrations before starting an agent.
- Executable profile registration refuses foreign command/registry symlinks and cross-harness prefix rebinding; replacement requires removing the conflicting registration explicitly.
- Standalone source installation is disabled. `install.sh` exits before writing; Homebrew is the only supported installation and upgrade path.
- A workspace manager does not become the owner of launcher-managed auth, runtime homes, MCP, skills, hooks, approval modes, or observability merely because it starts `harness-exec`.

## Out of scope

The following generally belong upstream unless the launcher creates or worsens the issue:

- vulnerabilities in Claude Code, Codex CLI, Kiro CLI, Happy, Gum, or Node.js;
- model behavior, prompt injection, or unsafe generated code without a launcher boundary failure;
- compromised MCP servers or gateways configured by the user;
- access granted by an explicit runtime bypass or danger-full-access option;
- a malicious `launcher.env` inside a project the user knowingly registered as trusted.

If you are unsure whether a finding is in scope, use the private advisory form.

## Trusted preservation boundary

`harness-preserve` is a same-UID, one-shot execution boundary. It does not
attest caller identity or protect against replacement of trusted installed files.
